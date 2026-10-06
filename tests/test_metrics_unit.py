"""E10-S1: Prometheus metrics. A scrape test for each metric, through the real exposition format.

The gate and reviewer counters are also checked on real PostgreSQL in
``test_metrics_integration.py``; GPU readings use a recorded ``nvidia-smi`` output here."""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from cdi_adapter import metrics
from cdi_adapter.mlserve.backends import StubBackend
from cdi_adapter.mlserve.grammar import GrammarError
from cdi_adapter.webapp.jobs import Job, JobRunner, Stages


def scrape(registry: Any = REGISTRY) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Every sample of one scrape, parsed back from the text format Prometheus reads."""
    text = generate_latest(registry).decode()
    return {(s.name, tuple(sorted(s.labels.items()))): s.value
            for fam in text_string_to_metric_families(text) for s in fam.samples}  # fmt: skip


def value(name: str, registry: Any = REGISTRY, **labels: str) -> float:
    return scrape(registry).get((name, tuple(sorted(labels.items()))), 0.0)


# ------------------------------------------------------------------ pipeline stages + queue depth


def _stages(extract_delay: float = 0.0, fail: str | None = None) -> Stages:
    def maybe_fail(stage: str) -> None:
        if fail == stage:
            raise RuntimeError(f"{stage} broke")

    def ingest(raw, name, mime, patient_id=None):
        maybe_fail("ingest")
        return SimpleNamespace(document_id=f"doc-{name}")

    def extract(doc, patient_id=None, expected_patient=None):
        time.sleep(extract_delay)
        maybe_fail("extract")
        return {"patient_id": patient_id or "p-1", "facts": 1}

    return Stages(ingest=ingest, ocr=lambda d, force_engine=None: maybe_fail("ocr"),
                  classify=lambda d: {"doc_type": "vitals_sheet", "is_handwritten": False},
                  extract=extract, terminology=lambda d: maybe_fail("terminology"),
                  validate=lambda d: maybe_fail("validate"),
                  counts=lambda d: {"facts": 1, "accepted": 0, "in_review": 1},
                  merge_identity=lambda p: None)  # fmt: skip


def test_every_stage_of_a_job_is_timed():
    before = {s: value("cdi_stage_seconds_count", stage=s) for s in metrics.STAGES}
    runner = JobRunner(_stages())
    job = runner.wait(runner.create([("a", b"x", None), ("b", b"x", None)]).id)
    assert job.state == "review"
    for s in metrics.STAGES:
        assert value("cdi_stage_seconds_count", stage=s) == before[s] + 2, s
    assert value("cdi_stage_seconds_bucket", stage="extract", le="+Inf") >= 2


@pytest.mark.parametrize("stage", ["ingest", "extract", "validate"])
def test_a_stage_that_raises_counts_a_failure(stage):
    before = value("cdi_stage_failures_total", stage=stage)
    runner = JobRunner(_stages(fail=stage))
    runner.wait(runner.create([("a", b"x", None)]).id)
    assert value("cdi_stage_failures_total", stage=stage) == before + 1


def test_queue_depth_counts_documents_waiting_for_a_gpu_slot():
    runner = JobRunner(_stages(extract_delay=0.3), extract_concurrency=1)
    files = [(n, b"x", None) for n in ("first", "a", "b", "c")]
    job = runner.create(files, patient_id="p-1")  # patient known: all four compete for the slot
    peak_wait = peak_flight = 0.0
    while job.state == "running":
        peak_wait = max(peak_wait, value("cdi_extract_waiting"))
        peak_flight = max(peak_flight, value("cdi_extract_in_flight"))
        time.sleep(0.02)
    assert peak_wait >= 2 and peak_flight == 1  # one GPU slot, the rest queue behind it
    assert value("cdi_extract_waiting") == 0 and value("cdi_extract_in_flight") == 0


def test_jobs_by_state():
    reg = CollectorRegistry()
    jobs = [Job(id="1", docs=[], state="running"), Job(id="2", docs=[], state="review"),
            Job(id="3", docs=[], state="review")]  # fmt: skip
    reg.register(metrics.JobsCollector(lambda: jobs))
    assert value("cdi_jobs", reg, state="running") == 1
    assert value("cdi_jobs", reg, state="review") == 2
    assert value("cdi_jobs", reg, state="mismatch") == 0  # every state is always present


def test_review_backlog_and_a_database_hiccup_never_breaks_the_scrape():
    reg = CollectorRegistry()
    reg.register(metrics.ReviewBacklogCollector(lambda: [("held_fact_tasks", 4), ("claimed", 1)]))
    assert value("cdi_review_backlog", reg, queue="held_fact_tasks") == 4

    def down() -> list[tuple[str, int]]:
        raise ConnectionError("db down")

    bad = CollectorRegistry()
    bad.register(metrics.ReviewBacklogCollector(down))
    assert not any(k[0] == "cdi_review_backlog" for k in scrape(bad))  # absent, not zero


# ------------------------------------------------------------------ GPU


SMI = "0, 87, 20480, 32607\n1, [N/A], 100, 24576\n"


def test_gpu_utilization_and_memory_from_nvidia_smi():
    reg = CollectorRegistry()
    reg.register(metrics.GpuCollector(lambda: SMI))
    assert value("cdi_gpu_utilization_percent", reg, gpu="0") == 87
    assert value("cdi_gpu_memory_used_bytes", reg, gpu="0") == 20480 * 1024 * 1024
    assert value("cdi_gpu_memory_total_bytes", reg, gpu="0") == 32607 * 1024 * 1024
    assert ("cdi_gpu_utilization_percent", (("gpu", "1"),)) not in scrape(reg)  # [N/A] skipped


def test_no_gpu_means_no_gpu_metrics_not_a_failed_scrape():
    def missing() -> str:
        raise FileNotFoundError("nvidia-smi")

    reg = CollectorRegistry()
    reg.register(metrics.GpuCollector(missing))
    assert not any(k[0].startswith("cdi_gpu") for k in scrape(reg))


# ------------------------------------------------------------------ model gateway


def test_gateway_metrics_endpoint_times_calls_and_counts_outcomes():
    from cdi_adapter.mlserve.app import create_app

    class Refusing(StubBackend):
        def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
            if prompt == "refuse":
                raise GrammarError("schema cannot be grammar-locked exactly")
            return super().generate(prompt, image_png, max_tokens=max_tokens,
                                    json_schema=json_schema)  # fmt: skip

    c = TestClient(create_app(Refusing()))
    ok0 = value("cdi_vlm_requests_total", kind="single", outcome="ok")
    ref0 = value("cdi_vlm_requests_total", kind="single", outcome="refused")
    bat0 = value("cdi_vlm_generate_seconds_count", kind="batch")
    assert c.post("/vlm/generate", json={"prompt": "hello"}).status_code == 200
    assert c.post("/vlm/generate", json={"prompt": "refuse"}).status_code == 422
    batch = {"items": [{"prompt": "a"}, {"prompt": "b"}]}
    assert c.post("/vlm/generate_batch", json=batch).status_code == 200
    r = c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    samples = {(s.name, tuple(sorted(s.labels.items()))): s.value
               for f in text_string_to_metric_families(r.text) for s in f.samples}  # fmt: skip
    key = ("cdi_vlm_requests_total", (("kind", "single"), ("outcome", "ok")))
    assert samples[key] == ok0 + 1
    assert (
        samples[("cdi_vlm_requests_total", (("kind", "single"), ("outcome", "refused")))]
        == ref0 + 1
    )
    assert samples[("cdi_vlm_generate_seconds_count", (("kind", "batch"),))] == bat0 + 1


# ------------------------------------------------------------------ web app exposure


def test_web_app_metrics_are_on_a_loopback_port_and_not_on_the_staff_app(monkeypatch):
    import socket

    from cdi_adapter.webapp.app import app, start_metrics
    from cdi_adapter.webapp.jobstore import PgJobStore

    monkeypatch.setattr(PgJobStore, "metric_rows", lambda self: [])  # no database here

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    start_metrics(port)
    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    for name in ("cdi_stage_seconds", "cdi_extract_waiting", "cdi_extract_in_flight", "cdi_jobs",
                 "cdi_gate_decisions_total", "cdi_review_decisions_total",
                 "cdi_review_items_total"):  # fmt: skip
        assert f"# TYPE {name.removesuffix('_total')}" in body, name
    # the staff-facing app has no metrics route: it is not in the fail-closed policy table
    r = TestClient(app).get("/metrics", follow_redirects=False)
    assert r.status_code in (403, 404) and "cdi_stage_seconds" not in r.text


def test_start_metrics_with_port_zero_serves_nothing():
    from cdi_adapter.webapp.app import start_metrics

    threads = threading.active_count()
    start_metrics(0)
    assert threading.active_count() == threads


# ------------------------------------------------------------------ label hygiene (no PHI)

ALLOWED_LABELS = {
    "stage",
    "state",
    "queue",
    "decision",
    "action",
    "kind",
    "outcome",
    "gpu",
    "le",
    "where",
    "fact_type",
    "role",
}


def test_metric_labels_come_from_fixed_lists_only():
    """A patient, document, file or user name can never become a label (§12, no PHI)."""
    names = {k[0] for k in scrape()} | {k[0] for k in scrape()}
    cdi = [k for k in scrape() if k[0].startswith("cdi_")]
    assert cdi, names
    for _, labels in cdi:
        assert {name for name, _ in labels} <= ALLOWED_LABELS, labels


def test_the_web_app_policy_has_no_public_metrics_route():
    from cdi_adapter.webapp.auth import POLICY

    assert not any(path == "/metrics" for _, path in POLICY)


def test_loopback_only_scrape_url_refuses_other_interfaces():
    """``start_http_server(addr="127.0.0.1")``: nothing listens on the machine's other addresses."""
    import socket

    from cdi_adapter.webapp.app import start_metrics

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    start_metrics(port)
    host = socket.gethostbyname(socket.gethostname())
    if host.startswith("127."):
        pytest.skip("this machine has no non-loopback address")
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        urllib.request.urlopen(f"http://{host}:{port}/metrics", timeout=2)
