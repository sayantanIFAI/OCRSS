"""Model gateway (ARCHITECTURE §3.4): one long-lived process owns the model.

POST /vlm/generate  {image_b64, prompt, max_tokens, json_schema?} -> {text, backend, model, usage}
GET  /healthz       -> { status, backend, model, device, loaded, configured_backend }
"""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cdi_adapter.config import get_settings
from cdi_adapter.logging import get_logger
from cdi_adapter.mlserve.backends import Backend, make_backend

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


def create_app(backend: Backend | None = None) -> FastAPI:
    app = FastAPI(title="cdi-adapter model gateway")
    app.state.backend = backend or make_backend()

    @app.post("/vlm/generate", response_model=GenerateResponse)
    def generate(req: GenerateRequest) -> GenerateResponse:
        image = None
        if req.image_b64:
            try:
                image = base64.b64decode(req.image_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="image_b64 is not valid base64"
                ) from exc
        be: Backend = app.state.backend
        t0 = time.perf_counter()
        text = be.generate(
            req.prompt, image, max_tokens=req.max_tokens, json_schema=req.json_schema
        )
        secs = round(time.perf_counter() - t0, 3)
        log.info("vlm_generate", backend=be.name, model=be.model, seconds=secs)
        return GenerateResponse(
            text=text,
            backend=be.name,
            model=be.model,
            usage={"prompt_chars": len(req.prompt), "output_chars": len(text), "seconds": secs},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        be: Backend = app.state.backend
        return {
            "status": "ok",
            "backend": be.name,
            "model": be.model,
            "device": be.device,
            "loaded": be.loaded,
            "configured_backend": get_settings().mlserve_backend,
        }

    return app


app = create_app()
