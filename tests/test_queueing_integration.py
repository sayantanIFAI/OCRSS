"""E9-S3 on real PostgreSQL: the identical upload sent twice is one job (even when both copies
arrive at once), and work that keeps failing leaves the queue for the dead-letter table.

The staged uploads are not what this block is about, so the object store here is a counter - what
matters is that a duplicate stages **nothing** and inserts **no** second job. The same two
properties through the real ``/api/jobs`` route, with a real MinIO, are in
``tests/test_apptier_integration.py``.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.webapp import jobstore
from cdi_adapter.webapp.jobs import DocState, Job, JobRunner, Stages
from cdi_adapter.webapp.jobstore import PgJobStore

pytestmark = pytest.mark.integration

LEASE = 60.0


@pytest.fixture(autouse=True)
def _no_background_runner():
    from cdi_adapter.webapp import jobs

    def clear() -> None:
        if jobs._runner is not None:
            jobs._runner.stop_background()
        jobs._runner = None

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _tidy(db_env):
    yield
    with session_scope() as c:
        ids = list(c.execute(text("SELECT id FROM web_job WHERE created_by LIKE 'test-q%'"
                                  )).scalars())  # fmt: skip
        if ids:
            c.execute(text("DELETE FROM dead_letter WHERE kind = 'web_job' AND ref = ANY(:i)"),
                      {"i": ids})  # fmt: skip
            c.execute(text("DELETE FROM web_job WHERE id = ANY(:i)"), {"i": ids})


class _Objects:
    """Counts what an upload stages, so a duplicate that stages nothing is visible."""

    def __init__(self) -> None:
        self.put: list[str] = []
        self.deleted: list[str] = []

    def put_bytes(self, key: str, raw: bytes, mime: str) -> None:
        self.put.append(key)

    def delete(self, key: str) -> None:
        self.deleted.append(key)


class _Store(PgJobStore):
    def __init__(self) -> None:
        self.objs = _Objects()
        super().__init__(objects=self.objs)  # type: ignore[arg-type]

    def files(self, job: Job, indices: list[int]) -> dict[int, tuple[str, bytes, str | None]]:
        return {i: (job.docs[i].filename, b"stub", "application/pdf") for i in indices}


def _files(tag: str) -> list[tuple[str, bytes, str | None]]:
    return [(f"rx-{tag}.pdf", f"%PDF-1.4 {tag}".encode(), "application/pdf")]


def _stages(ran: dict[str, int]) -> Stages:
    def ingest(raw: bytes, filename: str, mime: str | None, **kw: Any) -> Any:
        ran[filename] = ran.get(filename, 0) + 1
        return type("Res", (), {"document_id": uuid.uuid4()})()

    return Stages(
        ingest=ingest,
        ocr=lambda doc_id, **kw: None,
        classify=lambda doc_id: {"doc_type": "prescription"},
        extract=lambda doc_id, **kw: {},
        terminology=lambda doc_id: None,
        validate=lambda doc_id: None,
        counts=lambda doc_id: {"facts": 1, "accepted": 1, "in_review": 0},
        merge_identity=lambda pid: None,
    )


def _rows(key: str) -> int:
    with session_scope() as c:
        return int(c.execute(text("SELECT count(*) FROM web_job WHERE idempotency_key = :k"),
                             {"k": key}).scalar_one())  # fmt: skip


# ------------------------------------------------------------------ idempotency


def test_the_identical_upload_sent_twice_is_one_job(db_env, monkeypatch) -> None:
    """The acceptance case: a network retry of the same multipart POST. One job outcome, not two -
    and the retry stages nothing, so it costs the object store nothing either."""
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id, priority=None: None)
    st, tag = _Store(), uuid.uuid4().hex[:8]
    files = _files(tag)

    first = jobstore.submit(files, actor="test-q1", st=st)
    again = jobstore.submit(files, actor="test-q1", st=st)

    assert again.id == first.id
    assert _rows(jobstore.idempotency_key(files, actor="test-q1")) == 1
    assert len(st.objs.put) == 1 and st.objs.deleted == []
    with session_scope() as c:
        assert c.execute(text("SELECT count(*) FROM web_job WHERE created_by = 'test-q1'"
                              )).scalar_one() == 1  # fmt: skip


def test_two_copies_arriving_at_once_still_make_one_job(db_env, monkeypatch) -> None:
    """Both copies miss the lookup and both stage; the unique index decides, and the loser drops
    its staged files and answers with the winner's job."""
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id, priority=None: None)
    st, tag = _Store(), uuid.uuid4().hex[:8]
    files = _files(tag)
    start, out = threading.Barrier(2), []

    def go() -> None:
        start.wait(10)
        out.append(jobstore.submit(files, actor="test-q2", st=st))

    ts = [threading.Thread(target=go, daemon=True) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    assert len(out) == 2 and out[0].id == out[1].id
    assert _rows(jobstore.idempotency_key(files, actor="test-q2")) == 1
    assert len(st.objs.deleted) == len(st.objs.put) - 1  # exactly the loser's staged copy


def test_a_different_upload_is_still_its_own_job(db_env, monkeypatch) -> None:
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id, priority=None: None)
    st, tag = _Store(), uuid.uuid4().hex[:8]
    a = jobstore.submit(_files(tag), actor="test-q3", st=st)
    b = jobstore.submit(_files(tag + "x"), actor="test-q3", st=st)
    c_ = jobstore.submit(_files(tag), actor="test-q3b", st=st)  # same scan, another user
    assert len({a.id, b.id, c_.id}) == 3


def test_a_client_key_makes_the_retry_explicit(db_env, monkeypatch) -> None:
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id, priority=None: None)
    st, tag = _Store(), uuid.uuid4().hex[:8]
    first = jobstore.submit(_files(tag), actor="test-q4", given_key="counter-7", st=st)
    again = jobstore.submit(_files(tag), actor="test-q4", given_key="counter-7", st=st)
    other = jobstore.submit(_files(tag), actor="test-q4", given_key="counter-8", st=st)
    assert again.id == first.id and other.id != first.id


def test_the_lane_is_stored_and_read_back(db_env, monkeypatch) -> None:
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id, priority=None: None)
    st, tag = _Store(), uuid.uuid4().hex[:8]
    normal = jobstore.submit(_files(tag), actor="test-q5", st=st)
    urgent = jobstore.submit(_files(tag + "u"), actor="test-q5", priority=9, st=st)
    assert jobstore.priority_of(normal.id) == jobstore.NORMAL_PRIORITY
    assert jobstore.priority_of(urgent.id) == 9


def test_a_duplicate_is_not_dispatched_again(db_env, monkeypatch) -> None:
    """One job outcome means one run, so the retry must not put a second message on the queue."""
    sent: list[str] = []
    monkeypatch.setattr(jobstore, "dispatch", lambda job_id, priority=None: sent.append(job_id))
    st, tag = _Store(), uuid.uuid4().hex[:8]
    files = _files(tag)
    jobstore.submit(files, actor="test-q6", st=st)
    jobstore.submit(files, actor="test-q6", st=st)
    assert len(sent) == 1


# ------------------------------------------------------------------ dead letters


def _queued(st: PgJobStore, actor: str, name: str = "poison.pdf") -> Job:
    job = Job(id=uuid.uuid4().hex, docs=[DocState(filename=name)])
    st.insert(job, [{"filename": name, "mime": "application/pdf",
                     "key": f"uploads/{job.id}/00", "bytes": 4, "sha256": "0" * 64}],
              actor=actor, accepted_by="test", key=f"k-{job.id}")  # fmt: skip
    return job


def test_a_job_that_keeps_failing_lands_in_the_dead_letter_queue(db_env) -> None:
    """After `CDI_JOB_MAX_ATTEMPTS` the job is given up - and now it is on a list, with its
    attempt count and its documents' errors, instead of only a log line."""
    st = _Store()
    job = _queued(st, "test-q7")
    with session_scope() as c:  # it has already killed its runner three times
        c.execute(text("UPDATE web_job SET attempts = 3 WHERE id = :i"), {"i": job.id})

    ran: dict[str, int] = {}
    runner = JobRunner(_stages(ran), max_workers=2, store=st, owner="runner-A")
    assert runner.run_stored(job.id) == "error"
    assert ran == {}  # it was never run again

    with session_scope() as c:
        row = c.execute(text("SELECT * FROM dead_letter WHERE kind = 'web_job' AND ref = :r"),
                        {"r": job.id}).mappings().one()  # fmt: skip
        state = c.execute(text("SELECT state, dead_lettered_at, finished_at FROM web_job "
                               "WHERE id = :i"), {"i": job.id}).mappings().one()  # fmt: skip
    assert row["attempts"] == 4 and "gave up" in row["error"]
    assert row["queue"] == jobstore.WEB_QUEUE and row["replayed_at"] is None
    assert row["detail"]["documents"][0]["filename"] == "poison.pdf"
    assert state["dead_lettered_at"] is not None and state["finished_at"] is not None
    assert state["state"] == "error"


def test_a_dead_lettered_job_no_longer_blocks_the_queue(db_env) -> None:
    """It is finished, so no sweep picks it up again: the queue moves on."""
    st = _Store()
    job = _queued(st, "test-q8")
    with session_scope() as c:
        c.execute(text("UPDATE web_job SET attempts = 3 WHERE id = :i"), {"i": job.id})
    runner = JobRunner(_stages({}), max_workers=2, store=st, owner="runner-B")
    runner.run_stored(job.id)
    assert job.id not in st.orphans(0.0, limit=50)
    assert st.claim(job.id, "runner-C", LEASE) is None


def test_the_same_failure_updates_its_dead_letter_instead_of_piling_up(db_env) -> None:
    st = _Store()
    job = _queued(st, "test-q9")
    with session_scope() as c:
        first = repo.write_dead_letter(c, kind="web_job", ref=job.id, attempts=4,
                                       error="gave up", queue=jobstore.WEB_QUEUE)  # fmt: skip
        again = repo.write_dead_letter(c, kind="web_job", ref=job.id, attempts=7,
                                       error="gave up again", queue=jobstore.WEB_QUEUE)  # fmt: skip
        rows = c.execute(text("SELECT attempts, error, first_at, at FROM dead_letter "
                              "WHERE kind = 'web_job' AND ref = :r"),
                         {"r": job.id}).mappings().all()  # fmt: skip
    assert first == again and len(rows) == 1
    assert rows[0]["attempts"] == 7 and rows[0]["error"] == "gave up again"
    assert rows[0]["first_at"] <= rows[0]["at"]  # when the trouble started is kept


def test_what_was_given_up_can_be_listed_and_marked_replayed(db_env) -> None:
    st = _Store()
    job = _queued(st, "test-q10")
    with session_scope() as c:
        repo.write_dead_letter(c, kind="web_job", ref=job.id, attempts=4, error="gave up")
        assert any(r["ref"] == job.id for r in repo.dead_letters(c, kind="web_job"))
        assert repo.mark_replayed(c, kind="web_job", ref=job.id, actor="ops") is True
        assert not any(r["ref"] == job.id for r in repo.dead_letters(c, kind="web_job"))
        assert any(r["ref"] == job.id
                   for r in repo.dead_letters(c, kind="web_job", open_only=False))  # fmt: skip
        assert repo.mark_replayed(c, kind="web_job", ref=job.id, actor="ops") is False


def test_a_poison_document_is_dead_lettered_by_the_worker(db_env) -> None:
    """The folder-watch / ingest-API path: the stage that exhausts its retries records the
    document, not a job."""
    doc = str(uuid.uuid4())
    from cdi_adapter import worker

    worker.dead_letter("document", doc, task="cdi.extract_document", attempts=4,
                       error="ValueError: page 2 is not a PDF", queue="cdi-gpu",
                       detail={"stage": "extract"})  # fmt: skip
    try:
        with session_scope() as c:
            row = c.execute(text("SELECT * FROM dead_letter WHERE kind = 'document' AND ref = :r"),
                            {"r": doc}).mappings().one()  # fmt: skip
        assert row["task"] == "cdi.extract_document" and row["queue"] == "cdi-gpu"
        assert row["attempts"] == 4 and row["detail"]["stage"] == "extract"
    finally:
        with session_scope() as c:
            c.execute(text("DELETE FROM dead_letter WHERE kind = 'document' AND ref = :r"),
                      {"r": doc})  # fmt: skip


def test_an_unknown_dead_letter_kind_is_refused(db_env) -> None:
    with session_scope() as c, pytest.raises(ValueError, match="dead-letter kind"):
        repo.write_dead_letter(c, kind="whatever", ref="x", attempts=1)
