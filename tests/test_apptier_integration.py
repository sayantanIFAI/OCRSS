"""E10-S9 on real PostgreSQL (and real MinIO where marked): the lease rules of ``web_job``, an
upload that cannot be made durable is refused, any instance reads any job, and a runner that
dies mid-job is taken over without losing or doubling anything."""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from cdi_adapter.db import session_scope
from cdi_adapter.webapp.jobs import DocState, Job, JobRunner, Stages, phase1_done
from cdi_adapter.webapp.jobstore import PgJobStore

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_background_runner():
    """No process-wide runner (an in-process web app starts one) may sweep this module's jobs."""
    from cdi_adapter.webapp import jobs

    def clear() -> None:
        if jobs._runner is not None:
            jobs._runner.stop_background()
        jobs._runner = None

    clear()
    yield
    clear()


class _Objects:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


def _meta(job: Job) -> list[dict[str, Any]]:
    return [{"filename": d.filename, "mime": "application/pdf", "key": f"uploads/{job.id}/{i:02d}",
             "bytes": 1, "sha256": "0" * 64} for i, d in enumerate(job.docs)]  # fmt: skip


def _job(*names: str) -> Job:
    return Job(id=uuid.uuid4().hex, docs=[DocState(filename=n) for n in names])


def _expire(job_id: str) -> None:
    with session_scope() as c:
        c.execute(text("UPDATE web_job SET lease_until = now() - interval '1 second' "
                       "WHERE id = :i"), {"i": job_id})  # fmt: skip


def _drop(job_id: str) -> None:
    """A test's orphan must not be swept by a later test's runner."""
    with session_scope() as c:
        c.execute(text("DELETE FROM web_job WHERE id = :i"), {"i": job_id})


def test_the_lease_rules_on_postgres(db_env) -> None:
    objs = _Objects()
    st = PgJobStore(objects=objs)  # type: ignore[arg-type]
    job = _job("a.pdf")
    st.insert(job, _meta(job), actor="counter1", accepted_by="web1")
    loaded = st.load(job.id)
    assert loaded is not None and loaded.state == "running" and not loaded.finished.is_set()
    a = st.claim(job.id, "A", 30)
    assert a is not None and a.attempts == 1
    assert st.claim(job.id, "B", 30) is None  # a live lease
    a.docs[0].status, a.docs[0].step = "running", "ocr"
    assert st.save(a, "A") and not st.save(a, "B")
    assert st.heartbeat("A", 30) == 1 and st.heartbeat("B", 30) == 0
    assert job.id not in st.orphans(3600, 100)
    _expire(job.id)  # A died: nobody renews its lease
    assert job.id in st.orphans(3600, 100)
    assert any(r.orphaned for r in st.metric_rows())
    b = st.claim(job.id, "B", 30)
    assert b is not None and b.attempts == 2 and b.docs[0].step == "ocr"  # A's saved progress
    assert not st.save(a, "A")  # the old owner can no longer write
    b.state = "review"
    st.finish(b, "B")
    done = st.load(job.id)
    assert done is not None and done.finished.is_set() and done.state == "review"
    assert objs.deleted == [f"uploads/{job.id}/00"]  # the staged copy goes
    assert st.claim(job.id, "C", 30) is None  # finished: never run again
    with session_scope() as c:
        owner, until = c.execute(text("SELECT lease_owner, lease_until FROM web_job "
                                      "WHERE id = :i"), {"i": job.id}).one()  # fmt: skip
        actor = c.execute(text("SELECT actor FROM audit_log WHERE entity = 'web_job' "
                               "AND entity_id = :i"), {"i": job.id}).scalar_one()  # fmt: skip
    assert owner is None and until is None and actor == "counter1"


def test_a_never_claimed_job_is_an_orphan_after_the_unclaimed_time(db_env) -> None:
    st = PgJobStore(objects=_Objects())  # type: ignore[arg-type]
    job = _job("a.pdf")
    st.insert(job, _meta(job), actor="counter1", accepted_by="web1")
    assert job.id not in st.orphans(120, 1000)  # its dispatch may still be in the queue
    with session_scope() as c:
        c.execute(text("UPDATE web_job SET created_at = now() - interval '3 minutes' "
                       "WHERE id = :i"), {"i": job.id})  # fmt: skip
    assert job.id in st.orphans(120, 1000)  # the dispatch was lost: a sweep takes it
    _drop(job.id)


def test_the_gpu_pool_claiming_at_the_phase_boundary_is_not_another_attempt(db_env) -> None:
    """E9-S2: the CPU pool releases the job at the phase boundary and the GPU pool claims it. That
    is one run carrying on, so it must not spend one of CDI_JOB_MAX_ATTEMPTS - only a run that has
    to start over does. Everything else about the claim is unchanged."""
    st = PgJobStore(objects=_Objects())  # type: ignore[arg-type]
    job = _job("a.pdf")
    st.insert(job, _meta(job), actor="counter1", accepted_by="web1")
    cpu = st.claim(job.id, "cpu1", 30)
    assert cpu is not None and cpu.attempts == 1
    cpu.docs[0].status = "running"
    cpu.docs[0].document_id, cpu.docs[0].steps_done = "doc-1", ["ingest", "ocr", "classify"]
    assert st.save(cpu, "cpu1")
    st.release(job.id, "cpu1")  # the handoff: the lease goes, the queue message follows
    gpu = st.claim(job.id, "gpu1", 30, attempt=False)
    assert gpu is not None and gpu.attempts == 1  # still the first attempt
    assert phase1_done(gpu.docs[0])  # and Phase 1 came back with it
    assert st.claim(job.id, "gpu2", 30, attempt=False) is None  # the lease rules are unchanged
    _expire(job.id)
    again = st.claim(job.id, "gpu2", 30)  # a real retry still counts
    assert again is not None and again.attempts == 2
    _drop(job.id)


def test_a_released_lease_is_retried_at_once(db_env) -> None:
    st = PgJobStore(objects=_Objects())  # type: ignore[arg-type]
    job = _job("a.pdf")
    st.insert(job, _meta(job), actor="counter1", accepted_by="web1")
    assert st.claim(job.id, "A", 30) is not None
    st.release(job.id, "B")  # not the owner: no effect
    assert job.id not in st.orphans(3600, 1000)
    st.release(job.id, "A")
    assert job.id in st.orphans(3600, 1000)
    _drop(job.id)


def test_any_instance_reads_a_job_from_the_database(db_env) -> None:
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    st = PgJobStore(objects=_Objects())  # type: ignore[arg-type]
    job = _job("rx.pdf", "lab.pdf")
    st.insert(job, _meta(job), actor="counter1", accepted_by="web1")  # taken by "another" one
    got = signed_in(app, "admin").get(f"/api/jobs/{job.id}").json()
    assert got["state"] == "running" and got["patient"] is None
    assert [d["filename"] for d in got["documents"]] == ["rx.pdf", "lab.pdf"]


def test_an_upload_that_cannot_be_stored_is_refused_not_acknowledged(db_env, monkeypatch) -> None:
    from cdi_adapter import storage
    from cdi_adapter.config import get_settings
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    monkeypatch.setenv("CDI_S3_ENDPOINT_URL", "http://127.0.0.1:9")  # MinIO down
    get_settings.cache_clear()
    storage.get_store.cache_clear()
    with session_scope() as c:
        before = c.execute(text("SELECT count(*) FROM web_job")).scalar_one()
    try:
        r = signed_in(app, "intake").post(
            "/api/jobs", files=[("files", ("a.pdf", b"%PDF-1.4 x", "application/pdf"))]
        )
    finally:
        get_settings.cache_clear()
        storage.get_store.cache_clear()
    assert r.status_code == 503 and "NOT accepted" in r.json()["detail"]
    with session_scope() as c:
        assert c.execute(text("SELECT count(*) FROM web_job")).scalar_one() == before


def test_a_runner_killed_mid_job_is_taken_over_without_loss_or_doubles(
    app_env, monkeypatch
) -> None:
    """Real PostgreSQL + real MinIO + the real stages (stub model). Runner A takes the job and
    dies inside an extraction (it never returns: its lease runs out). Runner B's sweep takes
    the job over and finishes it; A's late end writes nothing."""
    from cdi_adapter.storage import get_store
    from cdi_adapter.webapp import jobstore
    from cdi_adapter.webapp.app import app
    from tests.helpers import make_pdf, signed_in
    from tests.test_validate_integration import _rx

    monkeypatch.setattr(jobstore, "dispatch", lambda job_id: None)  # the runners are ours
    tag = uuid.uuid4().hex[:6]
    pdfs = [make_pdf([_rx() + [f"Review in 2 weeks {tag}"]]),
            make_pdf([_rx() + [f"Follow-up visit {tag}"]])]  # fmt: skip
    r = signed_in(app, "admin").post(
        "/api/jobs",
        files=[("files", (f"rx{i}.pdf", p, "application/pdf")) for i, p in enumerate(pdfs)],
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    store = get_store()
    staged = [f"uploads/{job_id}/00", f"uploads/{job_id}/01"]
    assert all(store.exists(k) for k in staged)  # durable before the 202

    died = threading.Event()
    real = Stages.real()

    def dies_in_extract(*a: Any, **k: Any) -> Any:
        died.set()
        threading.Event().wait(20)  # the process is gone; its call never returns normally
        raise SystemError("killed")

    stages_a = Stages.real()
    stages_a.extract = dies_in_extract
    a = JobRunner(stages_a, max_workers=2, store=PgJobStore(), owner=f"A-{tag}")
    ta = threading.Thread(target=a.run_stored, args=(job_id,), daemon=True)
    ta.start()
    assert died.wait(60)
    mid = jobstore.load(job_id)
    assert mid is not None and not mid.finished.is_set()
    assert any(d.step == "extract" for d in mid.docs)  # the upload page saw it extracting
    _expire(job_id)  # A never renews again

    b = JobRunner(real, max_workers=2, store=PgJobStore(), owner=f"B-{tag}")
    assert job_id in b.sweep()
    done = jobstore.wait(job_id, timeout=300)
    assert done.state == "review" and done.attempts == 2
    assert [d.status for d in done.docs] == ["done", "done"], [d.error for d in done.docs]
    ta.join(30)  # A's call finally ends; it must write nothing
    after = jobstore.load(job_id)
    assert after is not None and after.state == "review" and after.attempts == 2
    with session_scope() as c:
        for d in done.docs:
            p = {"d": d.document_id}
            assert c.execute(text("SELECT count(*) FROM extraction WHERE document_id = :d"),
                             p).scalar_one() == 1  # fmt: skip
            assert (
                c.execute(
                    text(
                        "SELECT count(*) FROM rv_item WHERE source_document_id = :d "
                        "AND status IN ('workbasket', 'claimed')"
                    ),
                    p,
                ).scalar_one()
                == 1
            )
            facts = c.execute(text("SELECT count(*) FROM clinical_fact WHERE "
                                   "CAST(:d AS uuid) = ANY(source_doc_ids) AND is_current"),
                              p).scalar_one()  # fmt: skip
            assert facts == d.facts > 0  # the page's counts are the database's
    assert not any(store.exists(k) for k in staged)  # staged copies dropped at the end


def test_ingest_again_renders_the_pages_an_interrupted_ingest_never_stored(app_env) -> None:
    """A runner killed while rendering leaves a document without pages; the take-over's ingest
    of the same bytes renders them instead of reusing a document that has none."""
    from cdi_adapter.ingest.service import ingest_bytes
    from tests.helpers import make_pdf
    from tests.test_validate_integration import _rx

    pdf = make_pdf([_rx() + [f"Interrupted {uuid.uuid4().hex[:6]}"]])
    first = ingest_bytes(pdf, "rx.pdf", "application/pdf")
    assert first.page_count == 1 and not first.deduplicated
    with session_scope() as c:  # what the dead ingest left: the row, no pages
        c.execute(text("DELETE FROM document_page WHERE document_id = :d"),
                  {"d": first.document_id})  # fmt: skip
        c.execute(text("UPDATE source_document SET page_count = NULL, status = 'received' "
                       "WHERE id = :d"), {"d": first.document_id})  # fmt: skip
    again = ingest_bytes(pdf, "rx.pdf", "application/pdf")
    assert again.document_id == first.document_id and again.page_count == 1
    with session_scope() as c:
        pages = c.execute(text("SELECT count(*) FROM document_page WHERE document_id = :d"),
                          {"d": first.document_id}).scalar_one()  # fmt: skip
        status = c.execute(text("SELECT status FROM source_document WHERE id = :d"),
                           {"d": first.document_id}).scalar_one()  # fmt: skip
    assert pages == 1 and status == "pages_rendered"
    third = ingest_bytes(pdf, "rx.pdf", "application/pdf")  # complete now: plain dedupe
    assert third.deduplicated and third.document_id == first.document_id


def test_the_same_upload_posted_twice_is_one_job(app_env, monkeypatch) -> None:
    """E9-S3 acceptance through the real route, on real PostgreSQL + real MinIO: a counter whose
    POST timed out sends the identical multipart request again. It gets the first job's id back,
    no second job is created or dispatched, and the retry stages nothing new."""
    from cdi_adapter.storage import get_store
    from cdi_adapter.webapp import jobstore
    from cdi_adapter.webapp.app import app
    from tests.helpers import make_pdf, signed_in

    sent: list[str] = []
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id, priority=None: sent.append(job_id))
    tag = uuid.uuid4().hex[:6]
    pdf = make_pdf([[f"Amoxicillin 500mg {tag}"]])
    client = signed_in(app, "admin")
    files = [("files", (f"rx-{tag}.pdf", pdf, "application/pdf"))]

    first = client.post("/api/jobs", files=files)
    retry = client.post("/api/jobs", files=files)
    assert first.status_code == 202 and retry.status_code == 202, retry.text
    job_id = first.json()["job_id"]
    assert retry.json()["job_id"] == job_id  # exactly one job outcome, not two
    assert sent == [job_id]  # and one message on the queue

    with session_scope() as c:
        assert c.execute(text("SELECT count(*) FROM web_job WHERE id = :i"),
                         {"i": job_id}).scalar_one() == 1  # fmt: skip
        key = c.execute(text("SELECT idempotency_key FROM web_job WHERE id = :i"),
                        {"i": job_id}).scalar_one()  # fmt: skip
        assert key and c.execute(text("SELECT count(*) FROM web_job WHERE idempotency_key = :k"),
                                 {"k": key}).scalar_one() == 1  # fmt: skip
    store = get_store()
    assert store.exists(f"uploads/{job_id}/00")
    assert not store.exists(f"uploads/{job_id}/01")  # the retry staged nothing of its own

    # a client that names its retry gets the same answer, even for a different scan
    named = {"Idempotency-Key": f"counter-{tag}"}
    a = client.post("/api/jobs", files=files, headers=named)
    b = client.post("/api/jobs", files=[("files", (f"other-{tag}.pdf",
                                                   make_pdf([["Another scan"]]),
                                                   "application/pdf"))], headers=named)  # fmt: skip
    assert a.status_code == 202 and b.status_code == 202
    assert b.json()["job_id"] == a.json()["job_id"] != job_id
    _drop(job_id)
    _drop(a.json()["job_id"])
