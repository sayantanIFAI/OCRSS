"""E0-S15 unit: Celery chain (eager mode), folder watcher (real filesystem + watchdog), ingest API.

The stage services are replaced by recorders here so the *chain wiring* is tested on its own;
each service is covered by its own block. A real Redis round trip needs a Redis server (none is
published for Windows) and runs on the pod (E0-S16).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from watchdog.observers import Observer

from cdi_adapter import worker
from cdi_adapter.ingest import watcher
from tests.helpers import make_pdf


@pytest.fixture
def eager(monkeypatch):
    worker.celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
    monkeypatch.setattr(worker, "RETRY_BACKOFF", 0)
    yield
    worker.celery_app.conf.update(task_always_eager=False, task_eager_propagates=False)


@pytest.fixture
def stages(monkeypatch):
    calls: list[tuple] = []

    class Res:
        document_id, deduplicated = "doc-1", False

    def ingest_bytes(raw, name, **kw):
        calls.append(("ingest", name, kw.get("source_channel")))
        return Res()

    monkeypatch.setattr("cdi_adapter.ingest.service.ingest_bytes", ingest_bytes)
    for mod, fn, tag in [
        ("cdi_adapter.classify.service", "classify_document", "classify"),
        ("cdi_adapter.ocr.service", "ocr_document", "ocr"),
        ("cdi_adapter.extract.service", "extract_document", "extract"),
        ("cdi_adapter.terminology.service", "bind_document", "terminology"),
        ("cdi_adapter.validate.service", "validate_document", "validate"),
    ]:
        monkeypatch.setattr(f"{mod}.{fn}", lambda d, _t=tag, **k: calls.append((_t, d)))
    return calls


def conf_route(name):
    return worker.celery_app.conf.task_routes[name]["queue"]


def test_task_names_queue_and_delivery_guarantees():
    names = {t for t in worker.celery_app.tasks if t.startswith("cdi.")}
    assert names == {
        "cdi.ingest_file",
        "cdi.classify_document",
        "cdi.ocr_document",
        "cdi.extract_document",
        "cdi.bind_document",
        "cdi.web_job",  # E10-S9: a web-app job, on its own queue cdi-web
        "cdi.medication_agent",  # E12-S4: the advisory medication agent, also on cdi-web
    }  # fmt: skip  (TECH_STACK §5)
    assert conf_route("cdi.medication_agent") == "cdi-web"
    conf = worker.celery_app.conf
    assert conf.task_default_queue == "cdi" and conf.task_acks_late is True
    assert conf.broker_url.startswith("redis://")
    assert all(worker.celery_app.tasks[n].max_retries == worker.MAX_RETRIES for n in names)


def test_submit_file_runs_the_whole_chain_in_order(eager, stages, tmp_path):
    f = tmp_path / "rx.pdf"
    f.write_bytes(b"%PDF-1.4")
    worker.submit_file(f)
    assert stages == [("ingest", "rx.pdf", "folder"), ("classify", "doc-1"), ("ocr", "doc-1"),
                      ("extract", "doc-1"), ("terminology", "doc-1")]  # fmt: skip


def test_submit_document_skips_ingest(eager, stages):
    worker.submit_document("doc-9")
    assert [c[0] for c in stages] == ["classify", "ocr", "extract", "terminology"]


class _Retry(Exception):
    pass


@pytest.mark.parametrize("retries,countdown", [(0, 10), (1, 20), (2, 40)])
def test_failing_stage_requests_a_backed_off_retry(monkeypatch, retries, countdown):
    """Our retry *policy*; redelivery itself is Celery + Redis (verified on the pod)."""
    asked = {}

    def boom(d, **k):
        raise ConnectionError("gateway busy")

    def fake_retry(**kw):
        asked.update(kw)
        raise _Retry()

    monkeypatch.setattr("cdi_adapter.extract.service.extract_document", boom)
    task = worker.extract_document
    monkeypatch.setattr(task, "retry", fake_retry)
    task.push_request(retries=retries)
    try:
        with pytest.raises(_Retry):
            task.run("doc-2")
    finally:
        task.pop_request()
    assert isinstance(asked["exc"], ConnectionError)
    assert asked["countdown"] == countdown and asked["max_retries"] == worker.MAX_RETRIES


def test_a_successful_stage_never_retries(monkeypatch):
    monkeypatch.setattr("cdi_adapter.extract.service.extract_document", lambda d, **k: None)
    monkeypatch.setattr(worker.extract_document, "retry",
                        lambda **kw: pytest.fail("retry must not be requested"))  # fmt: skip
    assert worker.extract_document.run("doc-4") == "doc-4"


def test_a_missing_file_is_not_retried(monkeypatch, tmp_path):
    def no_retry(**kw):
        pytest.fail("a vanished file cannot succeed later")

    monkeypatch.setattr(worker.ingest_file, "retry", no_retry)
    with pytest.raises(FileNotFoundError):
        worker.ingest_file.run(str(tmp_path / "gone.pdf"))


# ---------------------------------------------------------------- watcher (real filesystem)


def _dirs(tmp_path):
    d = [tmp_path / n for n in ("inbox", "processed", "failed")]
    for x in d:
        x.mkdir()
    return d


@pytest.fixture(autouse=True)
def fast_stability(monkeypatch):
    monkeypatch.setattr(watcher, "STABLE_INTERVAL", 0.05)


def test_document_is_moved_to_processed_and_queued(tmp_path):
    inbox, processed, failed = _dirs(tmp_path)
    queued = []
    h = watcher.InboxHandler(inbox, processed, failed, submit=queued.append)
    (inbox / "rx.pdf").write_bytes(make_pdf([["Rx"]]))
    dest = h.process(inbox / "rx.pdf")
    assert dest == processed / "rx.pdf" and queued == [dest]
    assert not (inbox / "rx.pdf").exists() and dest.read_bytes().startswith(b"%PDF")


def test_non_document_goes_to_failed_and_is_not_queued(tmp_path):
    inbox, processed, failed = _dirs(tmp_path)
    queued = []
    h = watcher.InboxHandler(inbox, processed, failed, submit=queued.append)
    (inbox / "notes.exe").write_bytes(b"MZ\x90\x00 binary")
    assert h.process(inbox / "notes.exe") == failed / "notes.exe" and queued == []


def test_broker_outage_parks_the_file_in_failed(tmp_path):
    inbox, processed, failed = _dirs(tmp_path)

    def down(path):
        raise ConnectionError("redis down")

    h = watcher.InboxHandler(inbox, processed, failed, submit=down)
    (inbox / "lab.pdf").write_bytes(b"%PDF-1.4 x")
    assert h.process(inbox / "lab.pdf") == failed / "lab.pdf"
    assert (failed / "lab.pdf").exists()  # never lost


def test_name_collisions_never_overwrite(tmp_path):
    inbox, processed, failed = _dirs(tmp_path)
    h = watcher.InboxHandler(inbox, processed, failed, submit=lambda p: None)
    for i in range(3):
        (inbox / "scan.pdf").write_bytes(b"%PDF-1.4 " + bytes([i]))
        h.process(inbox / "scan.pdf")
    assert sorted(p.name for p in processed.iterdir()) == ["scan.1.pdf", "scan.2.pdf", "scan.pdf"]


def test_waits_for_a_file_that_is_still_being_written(tmp_path):
    f = tmp_path / "growing.pdf"
    f.write_bytes(b"%PDF")

    def grow():
        for _ in range(5):
            time.sleep(0.03)
            with f.open("ab") as fh:
                fh.write(b"x" * 100)

    t = threading.Thread(target=grow)
    t.start()
    assert watcher.wait_until_stable(f, checks=3, interval=0.05)
    t.join()
    assert f.stat().st_size == 504


def test_real_observer_picks_up_a_dropped_file(tmp_path):
    inbox, processed, failed = _dirs(tmp_path)
    queued: list[Path] = []
    h = watcher.InboxHandler(inbox, processed, failed, submit=queued.append)
    obs = Observer()
    obs.schedule(h, str(inbox), recursive=False)
    obs.start()
    try:
        (inbox / "dropped.pdf").write_bytes(make_pdf([["dropped"]]))
        t0 = time.monotonic()
        while not queued and time.monotonic() - t0 < 10:
            time.sleep(0.05)
    finally:
        obs.stop()
        obs.join()
    assert queued == [processed / "dropped.pdf"]


# ---------------------------------------------------------------- ingest API


def test_ingest_api_rejects_empty_upload():
    from cdi_adapter.api import app

    c = TestClient(app)
    assert c.post("/ingest", files={"file": ("a.pdf", b"", "application/pdf")}).status_code == 422
    assert c.get("/healthz").json() == {"status": "ok"}
