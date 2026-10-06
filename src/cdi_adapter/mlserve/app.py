"""Model gateway (ARCHITECTURE §3.4): one long-lived process owns the model.

POST /vlm/generate  {image_b64, prompt, max_tokens, json_schema?} -> {text, backend, model, usage}
POST /vlm/generate_batch  {items: [<generate request>, ...]} -> {texts, backend, model, usage}
                    (backlog E2-S8: independent sequences in one batched pass, texts in order)
GET  /healthz       -> { status, backend, model, version, device, loaded, configured_backend }
                       (``version``: the registered model version this gateway was started
                       for, backlog E10-S5; null outside the registry)
GET  /metrics       -> Prometheus text: call timing + GPU utilization (backlog E10-S1); the
                       gateway listens on 127.0.0.1 only, so this is never public
"""

from __future__ import annotations

import base64
import binascii
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import BaseModel, Field

from cdi_adapter import metrics, tracing
from cdi_adapter.config import get_settings
from cdi_adapter.logging import get_logger
from cdi_adapter.mlserve.backends import Backend, GenItem, _is_oom, make_backend
from cdi_adapter.mlserve.grammar import GenerationError, GrammarError

log = get_logger(__name__)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    image_b64: str | None = None
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    json_schema: dict[str, Any] | None = None


class GenerateResponse(BaseModel):
    text: str
    backend: str
    model: str
    usage: dict[str, Any]


class BatchRequest(BaseModel):
    """Several independent sequences in one call (backlog E2-S8)."""

    items: list[GenerateRequest] = Field(min_length=1, max_length=8)


class BatchResponse(BaseModel):
    texts: list[str]
    backend: str
    model: str
    usage: dict[str, Any]


def _image(b64: str | None) -> bytes | None:
    if not b64:
        return None
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="image_b64 is not valid base64") from exc


def create_app(backend: Backend | None = None, *, version: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if close := getattr(app.state.backend, "close", None):
                from starlette.concurrency import run_in_threadpool

                await run_in_threadpool(close)

    app = FastAPI(
        telemetry={"tracing": False}, title="cdi-adapter model gateway", lifespan=lifespan
    )
    app.state.backend = backend or make_backend()
    app.state.version = version or get_settings().model_version or None
    metrics.register_once("gpu", metrics.GpuCollector())

    def locked(schema: dict[str, Any] | None) -> bool:
        be = app.state.backend
        return be.name == "vllm" and be.settings.vllm_guided and schema is not None

    @app.post("/vlm/generate", response_model=GenerateResponse)
    def generate(req: GenerateRequest, request: Request) -> GenerateResponse:
        image = _image(req.image_b64)
        be: Backend = app.state.backend
        t0 = time.perf_counter()
        try:
            with tracing.continued(
                request.headers,
                "vlm.generate",
                {"cdi.backend": be.name, "cdi.max_tokens": req.max_tokens},
            ):
                text = be.generate(
                    req.prompt, image, max_tokens=req.max_tokens, json_schema=req.json_schema
                )
        except GenerationError as exc:
            metrics.VLM_REQUESTS.labels("single", "error").inc()
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except TimeoutError as exc:
            metrics.VLM_REQUESTS.labels("single", "error").inc()
            raise HTTPException(status_code=504, detail="model generation timed out") from exc
        except GrammarError as exc:  # E2-S3: never served unconstrained instead
            metrics.VLM_REQUESTS.labels("single", "refused").inc()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            metrics.VLM_REQUESTS.labels("single", "error").inc()
            if _is_oom(exc):  # E10-S2 "GPU OOM" alert
                metrics.VLM_OOM.labels("generate").inc()
            raise
        secs = round(time.perf_counter() - t0, 3)
        metrics.VLM_SECONDS.labels("single").observe(secs)
        metrics.VLM_REQUESTS.labels("single", "ok").inc()
        log.info("vlm_generate", backend=be.name, model=be.model, seconds=secs)
        return GenerateResponse(
            text=text,
            backend=be.name,
            model=be.model,
            usage={
                "prompt_chars": len(req.prompt),
                "output_chars": len(text),
                "seconds": secs,
                **({"grammar_locked": True} if locked(req.json_schema) else {}),
            },
        )

    @app.post("/vlm/generate_batch", response_model=BatchResponse)
    def generate_batch(req: BatchRequest, request: Request) -> BatchResponse:
        items = [GenItem(prompt=r.prompt, image_png=_image(r.image_b64), max_tokens=r.max_tokens,
                         json_schema=r.json_schema) for r in req.items]  # fmt: skip
        be: Backend = app.state.backend
        t0 = time.perf_counter()
        try:
            with tracing.continued(
                request.headers,
                "vlm.generate_batch",
                {"cdi.backend": be.name, "cdi.items": len(items)},
            ):
                texts = be.generate_batch(items)
                if len(texts) != len(items) or any(not isinstance(t, str) for t in texts):
                    raise HTTPException(
                        status_code=502, detail="model batch response is incomplete"
                    )
        except GenerationError as exc:
            metrics.VLM_REQUESTS.labels("batch", "error").inc()
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except TimeoutError as exc:
            metrics.VLM_REQUESTS.labels("batch", "error").inc()
            raise HTTPException(status_code=504, detail="model generation timed out") from exc
        except GrammarError as exc:  # E2-S3: never served unconstrained instead
            metrics.VLM_REQUESTS.labels("batch", "refused").inc()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            metrics.VLM_REQUESTS.labels("batch", "error").inc()
            if _is_oom(exc):  # E10-S2 "GPU OOM" alert
                metrics.VLM_OOM.labels("generate").inc()
            raise
        secs = round(time.perf_counter() - t0, 3)
        metrics.VLM_SECONDS.labels("batch").observe(secs)
        metrics.VLM_REQUESTS.labels("batch", "ok").inc()
        log.info("vlm_generate_batch", backend=be.name, model=be.model, items=len(items),
                 seconds=secs)  # fmt: skip
        return BatchResponse(texts=texts, backend=be.name, model=be.model,
                             usage={"items": len(items), "seconds": secs,
                                    **({"grammar_locked": [locked(i.json_schema) for i in items]}
                                       if any(locked(i.json_schema) for i in items) else {})}
        )  # fmt: skip

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        be: Backend = app.state.backend
        return {
            "status": "ok",
            "backend": be.name,
            "model": be.model,
            "version": app.state.version,
            "device": be.device,
            "loaded": be.loaded,
            "configured_backend": get_settings().mlserve_backend,
        }

    return app


app = create_app()
