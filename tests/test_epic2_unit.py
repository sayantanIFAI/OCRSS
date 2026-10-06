"""Epic 2 serving failure, evidence-integrity and benchmark regressions (CPU only)."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from cdi_adapter.config import get_settings
from cdi_adapter.dslm import bench, quantize
from cdi_adapter.ml.client import HttpMLClient, JsonRequest
from cdi_adapter.mlserve import grammar
from cdi_adapter.mlserve.app import create_app
from cdi_adapter.mlserve.backends import StubBackend, VLLMBackend
from cdi_adapter.webapp.jobs import JobRunner, extract_slots
from tests.test_batch_unit import BatchRecorder
from tests.test_dslm_unit import VITALS_OK, _gpu, _result
from tests.test_jobs_unit import Recorder, _files, _s
from tests.test_vllm_unit import FakeEngine, _backend, _settings


@pytest.mark.parametrize("failure", ["engine", "processor"])
def test_failed_vllm_initialization_cleans_up_loop_and_engine(failure):
    loops, stopped = [], []

    def factory(mid, settings):
        loops.append(asyncio.get_running_loop())
        if failure == "engine":
            raise ValueError("engine init failed")
        return SimpleNamespace(shutdown=lambda: stopped.append(True))

    def builder(mid):
        raise ValueError("processor init failed")

    be = VLLMBackend(_settings(), engine_factory=factory, prompt_builder=builder)
    for _ in range(2):
        with pytest.raises(ValueError, match="init failed"):
            be.generate("request")
    assert all(loop.is_closed() for loop in loops)
    assert not be.loaded
    assert len(stopped) == (2 if failure == "processor" else 0)


def test_generation_deadline_cancels_work_and_gateway_shutdown_closes_loop():
    engine = FakeEngine(delay=10)
    be, _ = _backend(engine)
    be.settings.vllm_request_timeout_s = 0.02
    with TestClient(create_app(be)) as client:
        result = client.post("/vlm/generate", json={"prompt": "hang"})
        assert result.status_code == 504
        deadline = time.monotonic() + 2
        while engine.in_flight and time.monotonic() < deadline:
            time.sleep(0.005)
        assert engine.in_flight == 0
        thread, loop = be._loop_thread, be._loop
    assert not thread.is_alive() and loop.is_closed() and not be.loaded
    be.close()  # shutdown is idempotent


def test_default_prompt_processor_uses_the_registered_revision(monkeypatch):
    from cdi_adapter.mlserve import backends

    seen = []
    monkeypatch.setattr(
        backends,
        "_default_prompt_builder",
        lambda model, *, revision: seen.append((model, revision)) or (lambda p, i: p),
    )
    be = VLLMBackend(
        _settings(vlm_model_revision="pinned-model-revision"),
        engine_factory=lambda mid, s: FakeEngine(),
        prompt_builder=backends._default_prompt_builder,
        sampling=lambda n, g: None,
    )
    try:
        be.generate("hello")
        assert seen == [(be.model, "pinned-model-revision")]
    finally:
        be.close()


@pytest.mark.parametrize("text", ['{"value":', '{"value": "wrong type"}', "NaN"])
def test_invalid_constrained_output_is_never_a_successful_response(text):
    class BrokenEngine:
        async def generate(self, request, params, request_id):
            yield SimpleNamespace(outputs=[SimpleNamespace(text=text)])

    be, _ = _backend(BrokenEngine(), grammar_builder=lambda s: "root ::= ...")
    schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    with TestClient(create_app(be)) as client:
        response = client.post("/vlm/generate", json={"prompt": "x", "json_schema": schema})
        assert response.status_code == 502
        assert "value" not in response.json()["detail"]  # no clinical output in errors


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "dependentRequired": {"a": ["b"]}},
        {"type": "object", "unevaluatedProperties": False},
        {"type": "array", "prefixItems": [{"type": "string"}]},
        {"const": "__cdi_unit_interval__"},
    ],
)
def test_unproven_schema_constraints_and_marker_collisions_fail_closed(schema):
    assert grammar.unsupported(schema)


def test_placeholder_rewrite_does_not_change_literal_data():
    schema = {"const": {"type": "number", "minimum": 0, "maximum": 1}}
    assert grammar.with_placeholders(schema) == schema


@pytest.mark.parametrize("batch", [False, True])
def test_grammar_locked_responses_skip_repair_and_retry(monkeypatch, batch):
    from cdi_adapter.ml import client as ml

    def no_repair(*args):
        pytest.fail("a grammar-locked response must not enter the repair path")

    monkeypatch.setattr(ml, "repair_payload", no_repair)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "text": VITALS_OK,
                "texts": [VITALS_OK, VITALS_OK],
                "usage": {"grammar_locked": [True, True] if batch else True},
            },
        )

    client = HttpMLClient("http://gateway", transport=httpx.MockTransport(handler))
    if batch:
        result = client.vlm_json_batch(
            [JsonRequest(None, "a", "vitals.v3"), JsonRequest(None, "b", "vitals.v3")]
        )
        assert result == [json.loads(VITALS_OK)] * 2
    else:
        assert client.vlm_json(None, "a", "vitals.v3") == json.loads(VITALS_OK)
    assert len(calls) == 1


def test_malformed_locked_response_fails_without_schema_repair_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"text": "{}", "usage": {"grammar_locked": True}})

    client = HttpMLClient("http://gateway", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="schema-invalid"):
        client.vlm_json(None, "a", "vitals.v3")
    assert len(calls) == 1


def test_hf_job_runner_cannot_override_the_serialization_guard(monkeypatch):
    monkeypatch.setenv("CDI_MLSERVE_BACKEND", "hf")
    get_settings.cache_clear()
    try:
        rec = Recorder(delay=0.03)
        runner = JobRunner(rec.stages(), extract_concurrency=4)
        runner.wait(runner.create(_files("a", "b", "c"), patient_id="p").id)
        assert runner.extract_slots == 1 and rec.max_p2 == 1
    finally:
        get_settings.cache_clear()
    assert (
        extract_slots(_s(mlserve_backend="vllm", extract_concurrency=8, vllm_max_num_seqs=4)) == 4
    )


def test_batch_gateway_rejects_missing_responses():
    class Missing(StubBackend):
        def generate_batch(self, items):
            return []

    with TestClient(create_app(Missing())) as client:
        assert (
            client.post(
                "/vlm/generate_batch", json={"items": [{"prompt": "a"}, {"prompt": "b"}]}
            ).status_code
            == 502
        )


def test_job_batch_count_mismatch_marks_every_document_failed():
    rec = BatchRecorder(shapes={"a": ("vitals_sheet", 1, 5), "b": ("vitals_sheet", 1, 5)})
    stages = rec.stages()
    stages.extract_batch = lambda *a, **k: []
    runner = JobRunner(stages, batch_max=2)
    job = runner.wait(runner.create(_files("a", "b"), patient_id="p").id)
    assert all(d.status == "error" and "wrong number" in d.error for d in job.docs)


@pytest.mark.parametrize(
    "mutation",
    ["digest", "schema", "id", "duplicate", "name", "engine", "gpu", "timing", "throughput"],
)
def test_benchmarks_refuse_incomparable_or_invalid_measurements(mutation):
    base = _result("base", 1.0, [VITALS_OK, VITALS_OK], _gpu()())
    other = copy.deepcopy(base)
    other["variant"] = "other"
    if mutation == "digest":
        other["corpus"]["digest"] = "b" * 64
    elif mutation == "schema":
        other["serial"][0]["schema"] = "prescription.v3"
    elif mutation == "id":
        other["serial"][0]["id"] = "wrong"
    elif mutation == "duplicate":
        other["serial"][1]["id"] = other["serial"][0]["id"]
    elif mutation == "name":
        other["variant"] = "base"
    elif mutation == "engine":
        other["engine"]["max_num_seqs"] = 8
    elif mutation == "gpu":
        other["gpu"]["name"] = "different GPU"
    elif mutation == "timing":
        other["extract_s_per_doc"]["median"] = float("nan")
    else:
        other["concurrency"][0]["docs_per_min"] = -1
    with pytest.raises(ValueError):
        bench.compare([base, other])


def test_benchmark_raw_schema_validity_does_not_salvage_prose():
    assert bench._parse("Here is your result: " + VITALS_OK, "vitals.v3")[1] is False


def test_benchmark_cli_fails_on_schema_invalid_output(tmp_path):
    paths = []
    for name, text in (("base", VITALS_OK), ("variant", "{}")):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(_result(name, 1.0, [text], _gpu()())))
        paths.append(str(path))
    assert bench.main(["compare", *paths]) == 1


def test_quantization_cannot_overwrite_source_or_existing_output(tmp_path):
    checkpoint, corpus = tmp_path / "model", tmp_path / "corpus"
    checkpoint.mkdir()
    corpus.mkdir()
    for out in (checkpoint, checkpoint / "quantized", corpus, tmp_path):
        with pytest.raises(ValueError):
            quantize.check_paths(str(checkpoint), corpus, out, samples=1, max_pixels=100)
    (checkpoint / "weights.bin").write_bytes(b"unchanged")
    assert (checkpoint / "weights.bin").read_bytes() == b"unchanged"


def test_start_script_refuses_a_different_running_backend(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\necho \'{"status":"ok","backend":"hf"}\'\n')
    curl.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "infra/runpod/start_mlserve.sh"
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REPO": str(tmp_path),
            "CDI_MLSERVE_BACKEND": "vllm",
        },
    )
    assert result.returncode == 1 and "backend mismatch" in result.stdout


def test_confidence_replacement_handles_properties_named_like_schema_keywords():
    schema = {"type": "object", "properties": {"const": grammar.UNIT_INTERVAL}}
    replaced = grammar.with_placeholders(schema)
    assert replaced["properties"]["const"] == {"const": grammar._MARK}
    assert grammar.unsupported({"minimum": 0, "maximum": 1})


def test_invalid_schema_is_refused_before_engine_initialization():
    with pytest.raises(grammar.GrammarError, match="invalid JSON schema"):
        grammar.schema_to_ebnf({"type": "not-a-json-type"})


def test_empty_schema_still_enables_grammar_and_metadata():
    seen = []
    be, engine = _backend(grammar_builder=lambda schema: seen.append(schema) or "grammar")
    with TestClient(create_app(be)) as client:
        result = client.post("/vlm/generate", json={"prompt": "x", "json_schema": {}})
        assert result.status_code == 200
        assert result.json()["usage"]["grammar_locked"] is True
    assert seen == [{}]
