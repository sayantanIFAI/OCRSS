"""E9-S8 on real PostgreSQL: the measurement the broker decision rests on.

The drill itself needs a running app tier, a real broker and the E9-S6 load (so it belongs on the
pod). What can be pinned down here is what its numbers *mean*: given jobs and documents in the
states a kill leaves behind, ``cdi_adapter.durability`` must count exactly what E9-S8 calls lost
and duplicated - and must not count a document the queue simply redelivered and ran again, which
is the queue working, not a failure.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from cdi_adapter import durability
from cdi_adapter.db import session_scope

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _tidy(db_env):
    yield
    with session_scope() as c:
        c.execute(text("DELETE FROM web_job WHERE created_by = 'test-durability'"))


def _job(docs: list[dict[str, Any]], *, finished: bool, attempts: int = 1,
         dead: bool = False, state: str = "review") -> str:  # fmt: skip
    """One ``web_job`` row in a state a kill could have left behind."""
    jid = uuid.uuid4().hex
    params = {"i": jid, "s": state if finished else "running", "d": json.dumps(docs),
              "a": attempts, "f": finished, "dl": dead, "k": f"body:{jid}"}  # fmt: skip
    with session_scope() as c:
        c.execute(
            text(
                "INSERT INTO web_job (id, state, files, docs, created_by, accepted_by, attempts, "
                "finished_at, dead_lettered_at, idempotency_key) VALUES (:i, :s, '[]'::jsonb, "
                "CAST(:d AS jsonb), 'test-durability', 'drill', :a, "
                "CASE WHEN :f THEN now() ELSE NULL END, "
                "CASE WHEN :dl THEN now() ELSE NULL END, :k)"
            ),
            params,
        )
    return jid


def _doc(status: str, document_id: str | None = None) -> dict[str, Any]:
    return {"filename": f"{status}.pdf", "status": status, "document_id": document_id,
            "steps_done": [], "error": None}  # fmt: skip


def _ingested() -> str:
    """A real ``source_document`` row, so extraction and review rows can point at it."""
    with session_scope() as c:
        return str(c.execute(text(
            "INSERT INTO source_document (sha256, filename, mime_type, byte_size, storage_key, "
            "source_channel, status) VALUES (:h, 'scan.pdf', 'application/pdf', 4, :k, "
            "'web', 'extracted') RETURNING id"),
            {"h": (uuid.uuid4().hex + uuid.uuid4().hex)[:64], "k": f"documents/{uuid.uuid4()}"},
        ).scalar_one())  # fmt: skip


def _extraction(doc_id: str) -> None:
    with session_scope() as c:
        c.execute(text(
            "INSERT INTO extraction (document_id, doc_type, schema_id, model_name, "
            "model_version, payload) VALUES (:d, 'prescription', 'prescription.v3', 'stub', "
            "'1', '{}'::jsonb)"), {"d": doc_id})  # fmt: skip


# ------------------------------------------------------------------ what is lost


def test_a_job_that_never_finished_is_lost(db_env) -> None:
    jid = _job([_doc("running")], finished=False)
    with session_scope() as c:
        lost = durability.lost(c, [jid])
    assert len(lost) == 1 and lost[0]["job_id"] == jid and lost[0]["status"] == "running"


def test_a_document_left_behind_in_a_finished_job_is_lost(db_env) -> None:
    """The job ended, but one of its documents never ran: that document is lost."""
    jid = _job([_doc("done", str(uuid.uuid4())), _doc("pending")], finished=True)
    with session_scope() as c:
        lost = durability.lost(c, [jid])
    assert [r["status"] for r in lost] == ["pending"]


def test_a_document_that_was_redelivered_and_ran_is_not_lost(db_env) -> None:
    """``attempts`` > 1 means a kill cost a take-over - which is the queue doing its job, so it is
    reported as resumed and never as a loss."""
    jid = _job([_doc("done", str(uuid.uuid4()))], finished=True, attempts=3)
    with session_scope() as c:
        assert durability.lost(c, [jid]) == []
        assert durability.resumed(c, [jid]) == 1


def test_a_document_that_failed_with_a_reason_is_not_lost(db_env) -> None:
    """A document the pipeline rejected reached a terminal state; it is not the broker's loss."""
    jid = _job([_doc("error", str(uuid.uuid4()))], finished=True, state="error")
    with session_scope() as c:
        assert durability.lost(c, [jid]) == []


def test_a_dead_lettered_job_is_reported_as_given_up_not_hidden(db_env) -> None:
    jid = _job([_doc("error")], finished=True, attempts=4, dead=True, state="error")
    with session_scope() as c:
        out = durability.measure(c, [jid])
    assert out["counts"]["given_up"] == 1
    assert out["ok"] is False  # the work ended without a result: never a pass


# ------------------------------------------------------------------ what is duplicated


def test_a_document_extracted_twice_is_duplicated(db_env) -> None:
    doc = _ingested()
    _extraction(doc)
    _extraction(doc)
    jid = _job([_doc("done", doc)], finished=True)
    with session_scope() as c:
        dup = durability.duplicated(c, durability.document_ids(c, [jid]))
    assert len(dup) == 1 and dup[0]["extractions"] == 2


def test_a_document_extracted_once_is_not_duplicated(db_env) -> None:
    doc = _ingested()
    _extraction(doc)
    jid = _job([_doc("done", doc)], finished=True)
    with session_scope() as c:
        assert durability.duplicated(c, durability.document_ids(c, [jid])) == []


def test_two_jobs_on_one_idempotency_key_would_be_the_clients_duplicate(db_env) -> None:
    """E9-S3's unique index makes this impossible; the drill checks it anyway, so a duplicate it
    finds cannot be blamed on a retried upload."""
    jid = _job([_doc("done", str(uuid.uuid4()))], finished=True)
    with session_scope() as c:
        assert durability.jobs_sharing_a_key(c, [jid]) == []
        with pytest.raises(Exception, match="duplicate key|unique"):
            c.execute(text("INSERT INTO web_job (id, state, files, docs, created_by, "
                           "idempotency_key) VALUES (:i, 'running', '[]'::jsonb, '[]'::jsonb, "
                           "'test-durability', :k)"),
                      {"i": uuid.uuid4().hex, "k": f"body:{jid}"})  # fmt: skip


# ------------------------------------------------------------------ the verdict


def test_a_clean_run_meets_the_tolerance(db_env) -> None:
    doc = _ingested()
    _extraction(doc)
    jobs = [_job([_doc("done", doc)], finished=True),
            _job([_doc("done", str(uuid.uuid4()))], finished=True, attempts=2)]  # fmt: skip
    with session_scope() as c:
        out = durability.measure(c, jobs)
    assert out["ok"] is True
    assert out["counts"] == {"lost": 0, "given_up": 0, "duplicated": 0, "jobs_sharing_a_key": 0}
    assert out["resumed"] == 1  # one job was taken over, and still ended once
    assert out["tolerance"] == {"max_lost": 0, "max_duplicated": 0, "max_given_up": 0}
    assert out["jobs"] == 2


def test_one_lost_document_fails_the_run(db_env) -> None:
    jobs = [_job([_doc("done", str(uuid.uuid4()))], finished=True),
            _job([_doc("running")], finished=False)]  # fmt: skip
    with session_scope() as c:
        out = durability.measure(c, jobs)
    assert out["ok"] is False and out["counts"]["lost"] == 1


def test_measuring_nothing_is_not_a_pass_by_accident(db_env) -> None:
    """An empty run has nothing wrong with it, but it also proves nothing - the drill's own
    ``kills_enough`` check is what stops a no-op run being read as evidence."""
    with session_scope() as c:
        out = durability.measure(c, [])
    assert out["jobs"] == 0 and out["documents"] == 0 and out["ok"] is True


def test_the_persistence_configuration_is_recorded_for_the_report(db_env) -> None:
    """E9-S8 names "Redis's default persistence configuration", so the numbers are read against
    it. No Redis here: the report then carries the reason instead of a silent blank."""
    out = durability.persistence("redis://127.0.0.1:6379/0")
    assert out["broker"] == "redis"
    assert "error" in out or "appendonly" in out
    amqp = durability.persistence("amqp://guest:guest@127.0.0.1:5672//")
    assert amqp["broker"] == "amqp" and amqp["confirm_publish"] is True
