"""E10-S3: one trace id follows a document from ``POST /api/jobs`` through every stage, the model
gateway (a separate process, reached over HTTP) and the FHIR projection, and every log line
carries it. End-to-end trace-continuity test, with an in-memory span exporter."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cdi_adapter import tracing
from cdi_adapter.logging import get_logger
from cdi_adapter.metrics import STAGES
from cdi_adapter.ml.client import HttpMLClient
from cdi_adapter.mlserve.app import create_app
from cdi_adapter.mlserve.backends import StubBackend
from cdi_adapter.webapp.jobs import JobRunner, Stages

EXPORTER = InMemorySpanExporter()


@pytest.fixture(autouse=True)
def spans():
    provider = tracing.setup("cdi-test")
    if not getattr(provider, "_cdi_test_exporter", False):
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        provider._cdi_test_exporter = True  # type: ignore[attr-defined]
    EXPORTER.clear()
    yield EXPORTER
    EXPORTER.clear()


VITALS = "\n".join(["[b1] City Clinic - Vital signs chart", "[b2] Pulse: 78 /min",
                    "[b3] BP: 128/82 mmHg"])  # fmt: skip


def _stages(gateway: HttpMLClient) -> Stages:
    """Fake database stages; the extract stage really calls the model gateway over HTTP."""
    log = get_logger("test.stage")

    def ingest(raw, name, mime, patient_id=None):
        log.info("ingested", filename_len=len(name))
        return SimpleNamespace(document_id=f"doc-{name}")

    def extract(doc, patient_id=None, expected_patient=None):
        payload = gateway.vlm_json(None, f"Extract.\n<document>\n{VITALS}\n</document>",
                                   "vitals.v3", retries=0)  # fmt: skip
        log.info("extracted", document_id=doc, vitals=len(payload.get("vitals", [])))
        return {"patient_id": patient_id or "patient-1", "facts": len(payload.get("vitals", []))}

    return Stages(ingest=ingest, ocr=lambda d, force_engine=None: log.info("ocr_done"),
                  classify=lambda d: {"doc_type": "vitals_sheet", "is_handwritten": False},
                  extract=extract, terminology=lambda d: log.info("bound"),
                  validate=lambda d: log.info("validated"),
                  counts=lambda d: {"facts": 2, "accepted": 0, "in_review": 2},
                  merge_identity=lambda p: None)  # fmt: skip


def _gateway() -> HttpMLClient:
    gw = TestClient(create_app(StubBackend()))
    return HttpMLClient("http://testserver", transport=gw._transport)


def _hex(i: int, width: int) -> str:
    return format(i, f"0{width}x")


def test_one_trace_follows_every_document_through_every_stage_the_gateway_and_fhir(
    spans, monkeypatch, capsys
):
    from cdi_adapter.webapp import app as webapp

    runner = JobRunner(_stages(_gateway()))
    job = runner.wait(runner.create([("a.pdf", b"x", None), ("b.pdf", b"x", None)]).id)
    assert job.state == "review" and job.trace_id and len(job.trace_id) == 32
    assert job.as_json()["trace_id"] == job.trace_id

    # the FHIR bundle is a later request: it joins the job's trace
    monkeypatch.setattr(webapp, "project_patient", lambda pid: {"bundles": [], "patient": pid})
    assert webapp._fhir(job)["patient"] == "patient-1"

    got = spans.get_finished_spans()
    assert {_hex(s.context.trace_id, 32) for s in got} == {job.trace_id}  # ONE trace
    by_id = {s.context.span_id: s for s in got}
    (root,) = [s for s in got if s.name == "job"]
    assert root.parent is None and root.attributes["cdi.job_id"] == job.id
    assert root.attributes["cdi.job_state"] == "review"

    for doc in ("doc-a.pdf", "doc-b.pdf"):  # every stage of every document, under the job
        stage_spans = {s.name.removeprefix("stage."): s for s in got
                       if s.name.startswith("stage.")
                       and s.attributes.get("cdi.document_id") in (doc, None)}  # fmt: skip
        mine = {s.name.removeprefix("stage."): s for s in got if s.name.startswith("stage.")
                and s.attributes.get("cdi.document_id") == doc}  # fmt: skip
        assert set(mine) == set(STAGES) - {"ingest"}, doc  # ingest runs before the id exists
        assert "ingest" in stage_spans
        for s in mine.values():
            assert s.parent.span_id == root.context.span_id

        # the gateway (reached over HTTP) continued the trace under this document's extract
        extract = mine["extract"]
        gw = [s for s in got if s.name == "vlm.generate"
              and s.parent.span_id == extract.context.span_id]  # fmt: skip
        assert len(gw) == 1 and gw[0].kind.name == "SERVER"

    (fhir,) = [s for s in got if s.name == "fhir.project"]
    assert fhir.parent.span_id == root.context.span_id  # the bundle ends the document's path

    # every log line of the job carries the job's trace id, and the stage it was logged in
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    ours = [ln for ln in lines if ln.get("event") in ("ingested", "ocr_done", "extracted",
                                                      "bound", "validated")]  # fmt: skip
    assert len(ours) == 2 * 5
    assert {ln["trace_id"] for ln in ours} == {job.trace_id}
    for ln in ours:
        span = by_id[int(ln["span_id"], 16)]
        assert span.name.startswith("stage.")  # logged inside its stage span


def test_two_jobs_are_two_traces(spans):
    runner = JobRunner(_stages(_gateway()))
    a = runner.wait(runner.create([("a.pdf", b"x", None)]).id)
    b = runner.wait(runner.create([("b.pdf", b"x", None)]).id)
    assert a.trace_id != b.trace_id
    for job in (a, b):
        ids = {_hex(s.context.trace_id, 32) for s in spans.get_finished_spans()
               if s.attributes.get("cdi.job_id") == job.id}  # fmt: skip
        assert ids == {job.trace_id}


def test_a_log_line_is_shipped_to_loki_with_its_trace_context(monkeypatch):
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

    exported = InMemoryLogRecordExporter()
    lp = LoggerProvider()
    lp.add_log_record_processor(SimpleLogRecordProcessor(exported))
    monkeypatch.setattr(tracing, "_log_handler", LoggingHandler(logging.INFO, logger_provider=lp))
    with tracing.span("stage.ocr", {"cdi.stage": "ocr"}) as s:
        get_logger("test").info("ocr_done", document_id="doc-1")
        get_logger("test").debug("too_quiet")  # below the handler's level: not shipped
    (rec,) = [r.log_record for r in exported.get_finished_logs()]
    assert _hex(rec.trace_id, 32) == tracing.ids(s)["trace_id"]
    assert _hex(rec.span_id, 16) == tracing.ids(s)["span_id"]
    body = json.loads(rec.body)
    assert body["event"] == "ocr_done" and body["document_id"] == "doc-1"
    assert body["trace_id"] == tracing.ids(s)["trace_id"]


def test_no_trace_ids_outside_a_span_and_export_is_off_by_default(capsys):
    from cdi_adapter.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.otel_traces_endpoint == "" and s.otel_logs_endpoint == ""
    get_logger("test").info("outside")
    line = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()
            if ln.startswith("{")][-1]  # fmt: skip
    assert line["event"] == "outside" and "trace_id" not in line


def test_span_attributes_are_ids_and_names_only(spans):
    """No patient name, file name or document text in a trace (§12): document ids are UUIDs,
    as ``ingest_bytes`` returns them."""
    import uuid

    stages = _stages(_gateway())
    stages.ingest = lambda raw, name, mime, patient_id=None: SimpleNamespace(
        document_id=str(uuid.uuid4())
    )
    runner = JobRunner(stages)
    runner.wait(runner.create([("Anjali Das rx.pdf", b"x", None)]).id)
    allowed = {"cdi.job_id", "cdi.job_state", "cdi.stage", "cdi.document_id", "cdi.document_ids",
               "cdi.backend", "cdi.max_tokens", "cdi.items"}  # fmt: skip
    finished = spans.get_finished_spans()
    assert finished
    for s in finished:
        assert set(s.attributes) <= allowed, (s.name, dict(s.attributes))
        values = json.dumps(dict(s.attributes), default=str)
        assert "Anjali" not in values and "rx.pdf" not in values, (s.name, values)
