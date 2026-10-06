"""E2-S8: short documents of one type share one batched model call.

Every document stays its own sequence (own prompt, image, schema, evidence map), so batching
may change speed but never what a document produces. Checked here at every layer: the gateway
endpoint, the hf / vllm backends, the client's retry path, the extract service's fallbacks and
the job runner's grouping. The speed-up itself is measured on the GPU (E1-S7 A/B)."""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cdi_adapter.config import Settings
from cdi_adapter.extract import service as extract_service
from cdi_adapter.extract.prompt import SCHEMA_FOR_DOC_TYPE, build_extraction_prompt
from cdi_adapter.extract.service import IdentityMismatch
from cdi_adapter.ml.client import HttpMLClient, JsonRequest, MLClient, StubMLClient
from cdi_adapter.mlserve.app import create_app
from cdi_adapter.mlserve.backends import GenItem, HFBackend, StubBackend, VLLMBackend
from cdi_adapter.mpi.service import Candidate
from cdi_adapter.webapp.jobs import JobRunner, Stages


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


VITALS = ["City Clinic - Vital signs chart", "Name: Test Patient Age: 40 Y",
          "Recorded on: 01/08/2026", "Pulse: 78 /min", "BP: 128/82 mmHg", "SpO2: 98 %",
          "Temperature: 98.4 F", "Allergies: Penicillin"]  # fmt: skip
LAB = ["City Lab - Laboratory report", "Name: Test Patient", "Reported on: 01/08/2026",
       "HbA1c 7.8 % (4.0-5.6) H", "Creatinine 0.9 mg/dL (0.6-1.2)"]  # fmt: skip


def _prompt(doc_type: str, lines: list[str]) -> str:
    return build_extraction_prompt(doc_type, [{"id": f"u{i}", "text": t}
                                              for i, t in enumerate(lines)])[0]  # fmt: skip


# ------------------------------------------------------------------ gateway


def test_the_batch_endpoint_answers_each_item_as_its_own_request() -> None:
    c = TestClient(create_app(StubBackend()))
    items = [{"prompt": _prompt("vitals_sheet", VITALS), "json_schema": {"$id": "cdi:vitals.v3"}},
             {"prompt": _prompt("lab_report", LAB), "json_schema": {"$id": "cdi:lab_report.v3"},
              "image_b64": base64.b64encode(_png()).decode()}]  # fmt: skip
    r = c.post("/vlm/generate_batch", json={"items": items})
    assert r.status_code == 200 and r.json()["usage"]["items"] == 2
    singles = [c.post("/vlm/generate", json=i).json()["text"] for i in items]
    assert r.json()["texts"] == singles  # same answers, same order
    assert '"heart_rate"' in singles[0] and '"HbA1c"' in singles[1]  # no cross-talk


def test_the_batch_endpoint_validates_like_the_single_one() -> None:
    c = TestClient(create_app(StubBackend()))
    bad = c.post("/vlm/generate_batch", json={"items": [{"prompt": "x", "image_b64": "!!"}]})
    assert bad.status_code == 422 and "base64" in bad.text
    assert c.post("/vlm/generate_batch", json={"items": []}).status_code == 422
    nine = [{"prompt": "x"}] * 9
    assert c.post("/vlm/generate_batch", json={"items": nine}).status_code == 422


# ------------------------------------------------------------------ backends


def _hf(batch_runner, runner=None) -> HFBackend:
    return HFBackend(Settings(_env_file=None, mlserve_backend="hf"),  # type: ignore[call-arg]
                     loader=lambda mid, s: ("M", "P"),
                     runner=runner or (lambda m, p, prompt, img, n: f"one:{prompt}"),
                     batch_runner=batch_runner)  # fmt: skip


def test_hf_runs_a_batch_as_one_forward_pass_under_the_gpu_lock() -> None:
    calls: list[list[str]] = []
    inflight, peak, lock = [0], [0], threading.Lock()

    def batch_runner(model, processor, items):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.05)
        with lock:
            inflight[0] -= 1
        calls.append([i.prompt for i in items])
        return [f"batch:{i.prompt}" for i in items]

    def runner(m, p, prompt, img, n):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.05)
        with lock:
            inflight[0] -= 1
        return f"one:{prompt}"

    hf = _hf(batch_runner, runner)
    items = [GenItem("a", max_tokens=100), GenItem("b", _png(), max_tokens=50)]
    t = threading.Thread(target=lambda: hf.generate("solo"))
    t.start()
    assert hf.generate_batch(items) == ["batch:a", "batch:b"]
    t.join()
    assert calls == [["a", "b"]]  # one pass for the whole batch
    assert peak[0] == 1  # still one forward pass at a time on the GPU
    assert hf.generate_batch([GenItem("c")]) == ["one:c"]  # a batch of one is a normal call


def test_hf_splits_a_batch_that_does_not_fit_instead_of_failing() -> None:
    class OutOfMemoryError(RuntimeError):
        pass

    def batch_runner(model, processor, items):
        raise OutOfMemoryError("CUDA out of memory")

    hf = _hf(batch_runner)
    assert hf.generate_batch([GenItem("a"), GenItem("b")]) == ["one:a", "one:b"]

    def broken(model, processor, items):
        raise ValueError("bad input")

    with pytest.raises(ValueError):  # anything else is a real error, not hidden
        _hf(broken).generate_batch([GenItem("a"), GenItem("b")])


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], Any]] = []
        self.inflight = self.peak = 0
        self.lock = threading.Lock()

    async def generate(self, request, params, request_id):
        import asyncio

        with self.lock:
            self.calls.append((request, params))
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.1)
            yield SimpleNamespace(prompt_token_ids=[1], outputs=[
                SimpleNamespace(text=json.dumps(f"answer:{request['prompt']}"), token_ids=[1])
            ])  # fmt: skip
        finally:
            with self.lock:
                self.inflight -= 1


def test_vllm_puts_every_batch_item_in_flight_together_each_with_its_grammar() -> None:
    engine = FakeEngine()
    be = VLLMBackend(Settings(_env_file=None, mlserve_backend="vllm"),  # type: ignore[call-arg]
                     engine_factory=lambda mid, s: engine,
                     prompt_builder=lambda mid: (lambda p, img: p),
                     sampling=lambda n, g=None: {"max_tokens": n, "grammar": g},
                     grammar_builder=lambda schema: f"G[{schema['$id']}]")  # fmt: skip
    items = [GenItem(f"p{i}", max_tokens=10 * (i + 1), json_schema={"$id": f"s{i}"})
             for i in range(3)]  # fmt: skip
    assert [json.loads(t) for t in be.generate_batch(items)] == [
        "answer:p0",
        "answer:p1",
        "answer:p2",
    ]
    assert engine.peak == 3
    params = {req["prompt"]: p for req, p in engine.calls}
    assert params["p2"] == {"max_tokens": 30, "grammar": "G[s2]"}


# ------------------------------------------------------------------ client


class ScriptedClient(MLClient):
    """generate_batch / generate answers from a script; records every call."""

    def __init__(self, batch: list[str], singles: list[str] | None = None) -> None:
        self.batch, self.singles = batch, list(singles or [])
        self.batch_calls: list[list[GenItem]] = []
        self.single_calls: list[str] = []

    def generate_batch(self, items):
        self.batch_calls.append(items)
        return self.batch

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
        self.single_calls.append(prompt)
        return self.singles.pop(0)


GOOD_VITALS = ('{"vitals": [{"name": "heart_rate", "value": "78", "unit": "/min", '
               '"evidence": ["b4"], "confidence": 0.9}], "confidence": 0.9}')  # fmt: skip


def test_client_batch_sends_one_call_and_only_reasks_the_invalid_answer() -> None:
    reqs = [JsonRequest(None, "first", "vitals.v3", 1200),
            JsonRequest(_png(), "second", "vitals.v3", 1200)]  # fmt: skip
    client = ScriptedClient(batch=[GOOD_VITALS, "not json at all"], singles=[GOOD_VITALS])
    out = client.vlm_json_batch(reqs)
    assert len(client.batch_calls) == 1
    assert [i.prompt for i in client.batch_calls[0]] == ["first", "second"]
    assert client.batch_calls[0][0].json_schema["$id"] == "cdi:vitals.v3"  # inlined, per item
    assert client.batch_calls[0][1].image_png == _png()
    # only the invalid one is asked again, with its errors, exactly as the single path does
    assert len(client.single_calls) == 1 and client.single_calls[0].startswith("second\n\n")
    assert "Your previous answer was invalid" in client.single_calls[0]
    assert [p["vitals"][0]["value"] for p in out] == ["78", "78"]
    assert not any(p.get("_partial") for p in out)


def test_client_batch_of_one_is_the_single_path() -> None:
    client = ScriptedClient(batch=[], singles=[GOOD_VITALS])
    out = client.vlm_json_batch([JsonRequest(None, "only", "vitals.v3")])
    assert client.batch_calls == [] and client.single_calls == ["only"]
    assert out[0]["vitals"][0]["name"] == "heart_rate"


def test_stub_batched_payloads_equal_single_payloads() -> None:
    stub = StubMLClient()
    reqs = [JsonRequest(None, _prompt("vitals_sheet", VITALS), "vitals.v3", 1200),
            JsonRequest(None, _prompt("lab_report", LAB), "lab_report.v3", 2000),
            JsonRequest(None, _prompt("vitals_sheet", VITALS[:4]), "vitals.v3", 1200)]  # fmt: skip
    singles = [stub.vlm_json(r.image_png, r.prompt, r.schema, max_tokens=r.max_tokens)
               for r in reqs]  # fmt: skip
    assert stub.vlm_json_batch(reqs) == singles


def test_http_client_posts_the_batch_and_checks_the_count() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen.append({"path": request.url.path, "body": body})
        n = len(body["items"]) if request.url.path.endswith("_batch") else 1
        return httpx.Response(200, json={"texts": ["x"] * n, "backend": "stub", "model": "m",
                                         "usage": {}})  # fmt: skip

    c = HttpMLClient("http://gw", transport=httpx.MockTransport(handler))
    assert c.generate_batch([GenItem("a", _png(), 10), GenItem("b")]) == ["x", "x"]
    body = seen[0]["body"]
    assert seen[0]["path"] == "/vlm/generate_batch"
    assert base64.b64decode(body["items"][0]["image_b64"]) == _png()
    assert body["items"][1]["image_b64"] is None and body["items"][0]["max_tokens"] == 10

    def short(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"texts": ["x"], "backend": "b", "model": "m"})

    with pytest.raises(ValueError, match="1 texts for 2 items"):
        HttpMLClient("http://gw", transport=httpx.MockTransport(short)).generate_batch(
            [GenItem("a"), GenItem("b")]
        )


# ------------------------------------------------------------------ extract service


class FakeFlow:
    """Replaces the extract service's database steps; records what happened to each document."""

    def __init__(self, monkeypatch, *, skip=(), bad_image=(), mismatch=()) -> None:
        self.events: list[tuple[str, str]] = []
        self.skip, self.bad_image, self.mismatch = set(skip), set(bad_image), set(mismatch)

        def start(doc, vlm):
            if doc in self.skip:
                return {"skipped": True, "doc_type": None, "facts": 0}
            return SimpleNamespace(document_id=doc, doc_type="vitals_sheet")

        def request(ctx, store):
            if ctx.document_id in self.bad_image:
                raise OSError("page image missing")
            return JsonRequest(None, f"prompt {ctx.document_id}", "vitals.v3", 1200)

        def complete(ctx, payload, vlm, *, patient_id, expected_patient, actor):
            if ctx.document_id in self.mismatch:
                raise IdentityMismatch(ctx.document_id, {}, Candidate(name="Other"))
            self.events.append(("complete", ctx.document_id))
            return {"document": ctx.document_id, "payload": payload, "patient_id": patient_id}

        def failure(ctx, exc, actor, vlm=None):
            self.events.append(("failed", ctx.document_id))

        monkeypatch.setattr(extract_service, "_start", start)
        monkeypatch.setattr(extract_service, "_request", request)
        monkeypatch.setattr(extract_service, "_complete", complete)
        monkeypatch.setattr(extract_service, "_record_failure", failure)


class CountingStub(StubMLClient):
    def __init__(self, fail_batch: bool = False) -> None:
        super().__init__()
        self.fail_batch, self.batches, self.singles = fail_batch, 0, 0

    def generate_batch(self, items):
        self.batches += 1
        if self.fail_batch:
            raise httpx.ConnectError("gateway down")
        return super().generate_batch(items)

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
        self.singles += 1
        return super().generate(prompt, image_png, max_tokens=max_tokens, json_schema=json_schema)


def test_extract_documents_isolates_each_documents_outcome(monkeypatch) -> None:
    flow = FakeFlow(monkeypatch, skip={"s"}, bad_image={"x"}, mismatch={"m"})
    client = CountingStub()
    out = extract_service.extract_documents(["a", "s", "x", "m", "b"], patient_id="p",
                                            client=client)  # fmt: skip
    assert client.batches == 1 and client.singles == 0  # a, m, b in one call
    assert out[0]["document"] == "a" and out[0]["patient_id"] == "p"
    assert out[1] == {"skipped": True, "doc_type": None, "facts": 0}
    assert isinstance(out[2], OSError) and isinstance(out[3], IdentityMismatch)
    assert out[4]["document"] == "b"
    assert ("failed", "x") in flow.events and ("failed", "m") in flow.events
    assert ("complete", "a") in flow.events and ("complete", "b") in flow.events


def test_a_failed_batch_call_falls_back_to_one_call_per_document(monkeypatch) -> None:
    FakeFlow(monkeypatch)
    client = CountingStub(fail_batch=True)
    out = extract_service.extract_documents(["a", "b", "c"], client=client)
    assert client.batches == 1 and client.singles == 3  # nothing lost
    assert [o["document"] for o in out] == ["a", "b", "c"]


def test_one_pending_document_is_not_batched(monkeypatch) -> None:
    FakeFlow(monkeypatch, skip={"s"})
    client = CountingStub()
    out = extract_service.extract_documents(["s", "a"], client=client)
    assert client.batches == 0 and client.singles == 1 and out[1]["document"] == "a"


# ------------------------------------------------------------------ job runner


class BatchRecorder:
    def __init__(self, *, shapes: dict[str, tuple[str, int, int]], mismatch=(),
                 batch_error: bool = False, shape_error=()) -> None:  # fmt: skip
        self.shapes, self.mismatch = shapes, set(mismatch)
        self.batch_error, self.shape_error = batch_error, set(shape_error)
        self.calls: list[tuple[Any, ...]] = []
        self.lock = threading.Lock()
        self.inflight = self.peak = 0

    def _enter(self) -> None:
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(0.03)
        with self.lock:
            self.inflight -= 1

    def stages(self) -> Stages:
        def ingest(raw, name, mime, patient_id=None):
            return SimpleNamespace(document_id=f"doc-{name}")

        def classify(doc):
            return {"doc_type": self.shapes[doc[4:]][0], "is_handwritten": False}

        def result(doc, patient_id):
            if doc[4:] in self.mismatch:
                return IdentityMismatch(doc, {}, Candidate(name="Someone Else"))
            return {"patient_id": patient_id or "patient-1", "facts": 2}

        def extract(doc, patient_id=None, expected_patient=None):
            self._enter()
            self.calls.append(("extract", doc))
            out = result(doc, patient_id)
            if isinstance(out, Exception):
                raise out
            return out

        def extract_batch(docs, patient_id=None, expected_patient=None):
            self._enter()
            self.calls.append(("batch", tuple(docs)))
            if self.batch_error:
                raise RuntimeError("gateway down")
            return [result(d, patient_id) for d in docs]

        def shape(doc):
            if doc[4:] in self.shape_error:
                raise RuntimeError("db hiccup")
            _, pages, blocks = self.shapes[doc[4:]]
            return {"pages": pages, "blocks": blocks}

        return Stages(ingest=ingest, ocr=lambda d, force_engine=None: None, classify=classify,
                      extract=extract, terminology=lambda d: None, validate=lambda d: None,
                      counts=lambda d: {"facts": 2, "accepted": 1, "in_review": 1},
                      merge_identity=lambda p: None, extract_batch=extract_batch,
                      shape=shape)  # fmt: skip


def _files(*names):
    return [(n, b"x", None) for n in names]


SHAPES = {
    "first": ("prescription", 1, 10),
    "v1": ("vitals_sheet", 1, 10), "v2": ("vitals_sheet", 1, 12), "v3": ("vitals_sheet", 1, 8),
    "v4": ("vitals_sheet", 1, 9),
    "l1": ("lab_report", 1, 20), "l2": ("lab_report", 1, 22),
    "long": ("lab_report", 1, 90),          # too many OCR blocks
    "multi": ("vitals_sheet", 3, 10),       # more than one page
}  # fmt: skip


def test_short_documents_of_one_type_share_a_call_the_rest_go_alone() -> None:
    rec = BatchRecorder(shapes=SHAPES)
    runner = JobRunner(rec.stages(), batch_max=3, batch_max_blocks=40)
    job = runner.wait(runner.create(_files("first", "v1", "l1", "v2", "long", "v3", "multi",
                                           "l2", "v4")).id)  # fmt: skip
    assert job.state == "review" and all(d.status == "done" for d in job.docs)
    # the first document still runs alone and fixes the patient (§7.2)
    assert rec.calls[0] == ("extract", "doc-first")
    batches = sorted(c[1] for c in rec.calls if c[0] == "batch")
    assert batches == [("doc-l1", "doc-l2"), ("doc-v1", "doc-v2", "doc-v3")]
    singles = {c[1] for c in rec.calls if c[0] == "extract"}
    assert singles == {"doc-first", "doc-long", "doc-multi", "doc-v4"}  # v4: the 4th vitals
    assert rec.peak == 1  # one slot (stub default): a batch is one GPU call like any other


def test_batching_is_off_by_default() -> None:
    assert Settings(_env_file=None).extract_batch_max == 1  # type: ignore[call-arg]
    rec = BatchRecorder(shapes=SHAPES)
    runner = JobRunner(rec.stages())
    runner.wait(runner.create(_files("first", "v1", "v2")).id)
    assert not any(c[0] == "batch" for c in rec.calls)


def test_a_mismatch_inside_a_batch_stops_the_job_like_a_single_one() -> None:
    rec = BatchRecorder(shapes=SHAPES, mismatch={"v2"})
    runner = JobRunner(rec.stages(), batch_max=3)
    job = runner.create(_files("first", "v1", "v2", "v3"), registry_patient={"name": "Ramesh"},
                        patient_id="registry-patient")  # fmt: skip
    job = runner.wait(job.id)
    assert job.state == "mismatch" and job.mismatch["document_name"] == "Someone Else"
    status = {d.filename: d.status for d in job.docs}
    assert status["v2"] == "mismatch" and status["v1"] == status["v3"] == "done"


def test_a_failed_batch_marks_its_documents_failed_and_the_job_continues() -> None:
    rec = BatchRecorder(shapes=SHAPES, batch_error=True)
    runner = JobRunner(rec.stages(), batch_max=3)
    job = runner.wait(runner.create(_files("first", "v1", "v2", "long")).id)
    status = {d.filename: d.status for d in job.docs}
    assert status == {"first": "done", "v1": "error", "v2": "error", "long": "done"}
    assert "gateway down" in job.docs[1].error and job.state == "review"


def test_a_document_whose_shape_is_unknown_is_extracted_alone() -> None:
    rec = BatchRecorder(shapes=SHAPES, shape_error={"v2"})
    runner = JobRunner(rec.stages(), batch_max=3)
    runner.wait(runner.create(_files("first", "v1", "v2", "v3")).id)
    assert ("extract", "doc-v2") in rec.calls
    assert ("batch", ("doc-v1", "doc-v3")) in rec.calls


def test_the_real_stages_wire_the_batched_extractor() -> None:
    real = Stages.real()
    assert real.extract_batch is extract_service.extract_documents and real.shape is not None
    assert SCHEMA_FOR_DOC_TYPE["vitals_sheet"] == "vitals.v3"
