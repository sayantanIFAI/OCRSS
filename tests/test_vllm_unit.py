"""E2-S1: the vLLM backend serves the same /vlm/generate contract, batches concurrent requests,
and ``hf`` is one env value away. vLLM itself (Linux + GPU) runs on the pod; here its
AsyncLLMEngine is replaced by a fake with the same streaming interface."""

from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cdi_adapter.config import Settings
from cdi_adapter.mlserve.app import create_app
from cdi_adapter.mlserve.backends import HFBackend, StubBackend, VLLMBackend, make_backend


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, mlserve_backend="vllm", **kw)  # type: ignore[call-arg]


class FakeEngine:
    """AsyncLLMEngine.generate(request, params, request_id) -> async stream of outputs."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay, self.calls, self.in_flight, self.max_in_flight = delay, [], 0, 0
        self.lock = threading.Lock()

    async def generate(self, request: dict[str, Any], params: Any, request_id: str):
        with self.lock:
            self.calls.append((request, params, request_id))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            text = (
                "{}" if params and params.get("grammar") else f"answer to {request['prompt'][-12:]}"
            )
            for i in (1, 2, 3):  # vLLM streams growing partial outputs; the last is final
                await asyncio.sleep(self.delay / 3)
                yield SimpleNamespace(outputs=[SimpleNamespace(text=text[: len(text) * i // 3])])
        finally:
            with self.lock:
                self.in_flight -= 1


def _backend(engine: FakeEngine | None = None, **kw: Any) -> tuple[VLLMBackend, FakeEngine]:
    engine = engine or FakeEngine()
    be = VLLMBackend(_settings(), engine_factory=lambda mid, s: engine,
                     prompt_builder=lambda mid: (lambda p, img: f"<chat img={img}>{p}"),
                     sampling=lambda n, g=None: {"max_tokens": n, "temperature": 0.0, "grammar": g},
                     **kw)  # fmt: skip
    return be, engine


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_generate_builds_the_chat_prompt_image_and_greedy_sampling() -> None:
    be, engine = _backend()
    assert not be.loaded  # lazy, like hf: the model loads on the first request
    out = be.generate("Extract the facts", _png(), max_tokens=256)
    assert be.loaded and out == "answer to ct the facts"
    request, params, rid = engine.calls[0]
    assert request["prompt"] == "<chat img=True>Extract the facts"
    assert isinstance(request["multi_modal_data"]["image"], Image.Image)
    assert params == {
        "max_tokens": 256,
        "temperature": 0.0,  # greedy, as hf do_sample=False
        "grammar": None,
    }  # no schema: free text
    be.generate("text only")
    assert "multi_modal_data" not in engine.calls[1][0]
    assert engine.calls[1][0]["prompt"].startswith("<chat img=False>")
    assert len({c[2] for c in engine.calls}) == 2  # a unique request id each


def test_concurrent_requests_are_in_flight_together() -> None:
    """Continuous batching needs requests to reach the engine at the same time (§3.4)."""
    be, engine = _backend(FakeEngine(delay=0.3))
    be.generate("warm up")
    with ThreadPoolExecutor(max_workers=4) as pool:
        t0 = time.perf_counter()
        results = list(pool.map(lambda i: be.generate(f"page {i}"), range(4)))
        wall = time.perf_counter() - t0
    assert len(results) == 4 and engine.max_in_flight == 4
    assert wall < 0.3 * 2  # not 4 x 0.3 s one after another


def test_hf_stays_one_forward_pass_at_a_time() -> None:
    inflight, peak, lock = [0], [0], threading.Lock()

    def runner(*a: Any) -> str:
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.05)
        with lock:
            inflight[0] -= 1
        return "ok"

    hf = HFBackend(Settings(_env_file=None, mlserve_backend="hf"),  # type: ignore[call-arg]
                   loader=lambda mid, s: ("M", "P"), runner=runner)  # fmt: skip
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: hf.generate("x"), range(4)))
    assert peak[0] == 1  # why Phase 2 is serialised with hf (§7.2)


def test_out_of_memory_falls_back_to_the_3b_model() -> None:
    class OutOfMemoryError(RuntimeError):
        pass

    engine, tried = FakeEngine(), []

    def factory(model_id: str, s: Settings) -> FakeEngine:
        tried.append(model_id)
        if "7B" in model_id:
            raise OutOfMemoryError("CUDA out of memory")
        return engine

    be = VLLMBackend(
        _settings(),
        engine_factory=factory,
        prompt_builder=lambda mid: lambda p, img: p,
        sampling=lambda n, g=None: None,
    )
    be.generate("x")
    assert tried == ["Qwen/Qwen2.5-VL-7B-Instruct", "Qwen/Qwen2.5-VL-3B-Instruct"]
    assert be.model == "Qwen/Qwen2.5-VL-3B-Instruct"


def test_other_start_up_errors_are_not_hidden() -> None:
    def factory(mid: str, s: Settings) -> Any:
        raise ValueError("bad engine args")

    be = VLLMBackend(
        _settings(),
        engine_factory=factory,
        prompt_builder=lambda mid: lambda p, img: p,
        sampling=lambda n, g=None: None,
    )
    with pytest.raises(ValueError, match="bad engine args"):
        be.generate("x")


def test_the_gateway_contract_is_unchanged_with_vllm() -> None:
    be, _ = _backend()
    c = TestClient(create_app(be))
    r = c.post("/vlm/generate", json={"prompt": "Read this", "max_tokens": 64,
                                      "image_b64": base64.b64encode(_png()).decode()})  # fmt: skip
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"text", "backend", "model", "usage"}
    assert body["backend"] == "vllm" and body["model"] == "Qwen/Qwen2.5-VL-7B-Instruct"
    health = c.get("/healthz").json()
    assert health["backend"] == "vllm" and health["loaded"] is True and health["device"] == "cuda"


def test_one_env_value_selects_the_backend() -> None:
    assert isinstance(make_backend(Settings(_env_file=None, mlserve_backend="vllm")), VLLMBackend)  # type: ignore[call-arg]  # noqa: E501
    assert isinstance(make_backend(Settings(_env_file=None, mlserve_backend="hf")), HFBackend)  # type: ignore[call-arg]  # noqa: E501
    assert isinstance(make_backend(Settings(_env_file=None, mlserve_backend="stub")), StubBackend)  # type: ignore[call-arg]  # noqa: E501
    with pytest.raises(ValueError):
        Settings(_env_file=None, mlserve_backend="tgi")  # type: ignore[call-arg]


def test_nothing_imports_vllm_until_the_backend_is_used() -> None:
    import sys

    VLLMBackend(_settings())  # constructing it must not need vllm (it is not on this machine)
    assert "vllm" not in sys.modules


# ---------------------------------------------------------------- pod packaging (own venv)

from pathlib import Path  # noqa: E402

from cdi_adapter.podmanifest import TORCH_FAMILY, parse_lock, parse_versions  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNPOD = ROOT / "infra" / "runpod"
PINS = parse_versions((RUNPOD / "versions.env").read_text(encoding="utf-8"))


def test_the_gateway_venv_is_locked_exactly_with_vllm_on_torch_2_8() -> None:
    text = (RUNPOD / "requirements-vllm.lock").read_text(encoding="utf-8")
    lock = parse_lock(text)
    assert lock["vllm"] == PINS["VLLM_VERSION"] == "0.11.0"
    assert lock["transformers"] == PINS["VLLM_TRANSFORMERS_VERSION"]
    assert lock["transformers"].startswith("4.")  # vLLM 0.11.0 predates transformers 5
    assert {"xgrammar", "xformers", "fastapi", "uvicorn", "structlog"} <= lock.keys()
    assert not [n for n in lock if TORCH_FAMILY.match(n)]  # torch comes from the image
    assert "torch==2.8.0" in text.split("\n\n", 1)[0]  # resolved against App. B's torch
    blocks = text.split("\n\n", 1)[1].strip().split("\n")
    assert sum(ln.startswith("    --hash=sha256:") for ln in blocks) >= len(lock)


def test_the_pipeline_venv_keeps_transformers_5() -> None:
    main = parse_lock((RUNPOD / "requirements.lock").read_text(encoding="utf-8"))
    assert main["transformers"] == PINS["TRANSFORMERS_VERSION"] == "5.16.1"
    assert "vllm" not in main  # vLLM never enters the pipeline venv


def test_the_pod_builds_and_checks_the_gateway_venv_only_for_vllm() -> None:
    boot = (RUNPOD / "bootstrap_pod.sh").read_text(encoding="utf-8")
    step = boot[boot.index("3b. vLLM gateway venv") : boot.index("4. Redis")]
    assert 'if [ "${CDI_MLSERVE_BACKEND:-stub}" = "vllm" ]; then' in step
    assert '--require-hashes --no-deps -r "$HERE/requirements-vllm.lock"' in step
    assert "--system-site-packages" in step and '"torchaudio==$TORCHAUDIO_VERSION"' in step
    drift = boot[boot.index("7. version manifest") :]
    assert '--lock "$HERE/requirements-vllm.lock"' in drift and "manifests-vllm" in drift
    start = (RUNPOD / "start_mlserve.sh").read_text(encoding="utf-8")
    assert "PY=$REPO/.venv-vllm/bin/python" in start and '"$PY" -m cdi_adapter.mlserve' in start
    env = (ROOT / ".env.runpod").read_text(encoding="utf-8")
    assert "\nCDI_MLSERVE_BACKEND=hf\n" in env  # hf stays the active backend (§3.4)


# ---------------------------------------------------------------- grammar-locked decoding (E2-S3)


def test_a_json_schema_is_sent_to_the_engine_as_a_grammar() -> None:
    built: list[dict[str, Any]] = []

    def builder(schema: dict[str, Any]) -> str:
        built.append(schema)
        return 'root ::= "{}"'

    be, engine = _backend(grammar_builder=builder)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    be.generate("Extract", json_schema=schema)
    assert built == [schema] and engine.calls[0][1]["grammar"] == 'root ::= "{}"'


def test_guided_decoding_can_be_switched_off() -> None:
    engine = FakeEngine()
    off = Settings(_env_file=None, mlserve_backend="vllm", vllm_guided=False)  # type: ignore[call-arg]
    be = VLLMBackend(off, engine_factory=lambda mid, s: engine,
                     prompt_builder=lambda mid: lambda p, i: p,
                     sampling=lambda n, g=None: {"grammar": g},
                     grammar_builder=lambda s: pytest.fail("no grammar when off"))  # fmt: skip
    be.generate("x", json_schema={"type": "object"})
    assert engine.calls[0][1] == {"grammar": None}


def test_a_schema_that_cannot_be_locked_is_refused_not_sent_loosely() -> None:
    from cdi_adapter.mlserve.grammar import GrammarError

    def builder(schema: dict[str, Any]) -> str:
        raise GrammarError("schema cannot be grammar-locked exactly: #/properties/x: format")

    be, engine = _backend(grammar_builder=builder)
    c = TestClient(create_app(be))
    r = c.post("/vlm/generate", json={"prompt": "x", "json_schema": {"type": "object"}})
    assert r.status_code == 422 and "format" in r.json()["detail"]
    assert engine.calls == [] and not be.loaded  # the engine was never asked


def test_the_engine_uses_xgrammar_with_no_fallback() -> None:
    from cdi_adapter.mlserve.backends import structured_output_config

    cfg = structured_output_config(_settings())
    assert cfg == {"backend": "xgrammar", "disable_fallback": True}


def test_the_client_sends_every_extraction_schema_inlined() -> None:
    """The gateway can only lock what it is sent: the client passes ``bundled_schema``."""
    import json

    from cdi_adapter.ml.client import MLClient, bundled_schema

    seen: list[Any] = []

    class Recording(MLClient):
        def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):  # type: ignore[no-untyped-def]
            seen.append(json_schema)
            return '{"doc_type": "prescription", "confidence": 0.9}'

    Recording().vlm_json(None, "classify", "classification.v1", retries=0)
    assert seen == [bundled_schema("classification.v1")]
    assert "$ref" not in json.dumps(seen[0])
