"""E9-S1 on real PostgreSQL: a schema change while the cluster is up.

A **forward-incompatible** Alembic migration (here: ``web_job.state`` renamed, plus a new NOT NULL
column) is applied and rolled back with jobs waiting in the queue and a runner in flight, and
every waiting job then runs - exactly once. The revision used is a real Alembic revision, written
into a temporary version location on top of the repository's head, so the test exercises
``cdi_adapter.migration`` the way the CLI does, and never leaves the test database off head.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from cdi_adapter import migration
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.webapp.jobs import DocState, Job, JobRunner, Stages
from cdi_adapter.webapp.jobstore import PgJobStore

pytestmark = pytest.mark.integration

LEASE = 60.0

# A forward-incompatible change: code that says "SET state = ..." breaks on it, which is the whole
# reason the queue has to be held while it is applied.
TEST_REVISION = '''"""test-only forward-incompatible change (tests/test_migration_integration.py)

Revision ID: t001
Revises: {head}
"""

from alembic import op

revision = "t001"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE web_job RENAME COLUMN state TO job_state")
    op.execute("ALTER TABLE web_job ADD COLUMN lifecycle text NOT NULL DEFAULT 'new'")


def downgrade() -> None:
    op.execute("ALTER TABLE web_job DROP COLUMN lifecycle")
    op.execute("ALTER TABLE web_job RENAME COLUMN job_state TO state")
'''


# ------------------------------------------------------------------ fixtures and helpers


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


@pytest.fixture(autouse=True)
def _tidy(db_env):
    """Whatever a test does, the next one starts with an open gate, no jobs of ours and the
    schema at the repository's head. Other modules intentionally leave unfinished runs;
    finish those test fixtures before asserting global in-flight/queue counts."""
    with session_scope() as c:
        c.execute(
            text(
                "UPDATE pipeline_run SET status = 'failed', finished_at = now() "
                "WHERE status = 'running'"
            )
        )
        c.execute(
            text(
                "UPDATE web_job SET state = 'error', finished_at = now(), lease_until = NULL "
                "WHERE finished_at IS NULL"
            )
        )
    yield
    with session_scope() as c:
        c.execute(text("UPDATE schema_gate SET closed = false, expires_at = NULL WHERE id = 1"))
        c.execute(text("DELETE FROM web_job WHERE created_by = 'test-migration'"))


@pytest.fixture
def test_cfg(db_env, tmp_path) -> Any:
    """An Alembic config whose versions are the repository's plus one throwaway revision on top.
    The schema is put back at the repository head when the test ends, whatever it did."""
    from tests.conftest import alembic_cfg

    head = migration.script_head(alembic_cfg(db_env))
    extra = tmp_path / "versions"
    extra.mkdir()
    (extra / "t001_test_only.py").write_text(TEST_REVISION.format(head=head), encoding="utf-8")
    cfg = alembic_cfg(db_env)
    repo_versions = Path(cfg.get_main_option("script_location")) / "versions"
    cfg.set_main_option("version_locations",
                        os.pathsep.join([str(repo_versions), str(extra)]))  # fmt: skip
    cfg.attributes["repo_head"] = head
    yield cfg
    if migration.db_revision() != head:  # a failed test must not leave the schema ahead
        from alembic import command

        command.downgrade(cfg, head)
    assert migration.db_revision() == head


class _Objects:
    """The staged uploads are not what this test is about; MinIO is not needed."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, key: str) -> None:
        self.deleted.append(key)


class _Store(PgJobStore):
    def __init__(self) -> None:
        super().__init__(objects=_Objects())  # type: ignore[arg-type]

    def files(self, job: Job, indices: list[int]) -> dict[int, tuple[str, bytes, str | None]]:
        return {i: (job.docs[i].filename, b"stub", "application/pdf") for i in indices}


def _queue(store: PgJobStore, name: str = "rx.pdf") -> Job:
    """One accepted, unclaimed job - a job waiting in the queue."""
    job = Job(id=uuid.uuid4().hex, docs=[DocState(filename=name)])
    store.insert(job, [{"filename": name, "mime": "application/pdf",
                        "key": f"uploads/{job.id}/00", "bytes": 4, "sha256": "0" * 64}],
                 actor="test-migration", accepted_by="test")  # fmt: skip
    return job


def _stages(ran: dict[str, int]) -> Stages:
    """Stub stages that record how many times each document was ingested: a job run twice would
    ingest its document twice."""

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


def _closed() -> bool:
    with session_scope() as c:
        return migration.is_closed(c)


def _wait(predicate: Any, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _column(name: str) -> bool:
    with session_scope() as c:
        return bool(c.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'web_job' "
            "AND column_name = :n"), {"n": name}).first())  # fmt: skip


# ------------------------------------------------------------------ the gate


def test_a_closed_gate_stops_new_claims_and_the_sweep(db_env) -> None:
    store = _Store()
    jobs = [_queue(store) for _ in range(3)]
    with session_scope() as c:
        migration.close_gate(c, reason="test", actor="test", hold_s=60)
    assert _closed()
    for job in jobs:
        assert store.claim(job.id, "runner-A", LEASE) is None  # the queue holds still
    assert store.orphans(0.0, limit=20) == []
    with session_scope() as c:
        assert migration.queued(c) >= 3  # nothing was lost, nothing was started
        assert migration.in_flight(c) == 0
        migration.open_gate(c, actor="test")
    for job in jobs:  # and now each of them is claimed, once
        assert store.claim(job.id, "runner-A", LEASE) is not None
        assert store.claim(job.id, "runner-B", LEASE) is None


def test_a_job_already_claimed_keeps_its_lease_while_the_gate_is_closed(db_env) -> None:
    """A migration must not interrupt a job that is running: only new claims are refused."""
    store = _Store()
    job = _queue(store)
    claimed = store.claim(job.id, "runner-A", LEASE)
    assert claimed is not None
    with session_scope() as c:
        migration.close_gate(c, reason="test", actor="test", hold_s=60)
        assert migration.in_flight(c) == 1
    assert store.heartbeat("runner-A", LEASE) == 1  # the runner keeps the job
    assert store.claim(job.id, "runner-B", LEASE) is None  # nobody takes it from under it
    store.finish(claimed, "runner-A")
    with session_scope() as c:
        assert migration.in_flight(c) == 0


def test_a_stage_still_running_on_the_folder_path_counts_as_in_flight(db_env) -> None:
    """The folder-watch / ingest-API chain holds no lease, so an open ``pipeline_run`` is what
    says a worker is mid-document - and a row a dead worker left behind does not hold a migration
    back for ever."""
    with session_scope() as c:
        run = c.execute(text("INSERT INTO pipeline_run (stage, status) VALUES ('extract', "
                             "'running') RETURNING id")).scalar_one()  # fmt: skip
    try:
        with session_scope() as c:
            assert migration.stages_in_flight(c) == 1
            assert migration.in_flight(c) == 1  # no web job, but a worker is busy
            c.execute(text("UPDATE pipeline_run SET started_at = now() - interval '2 days' "
                           "WHERE id = :i"), {"i": run})  # fmt: skip
            assert migration.stages_in_flight(c) == 0  # stale: that worker is gone
            c.execute(text("UPDATE pipeline_run SET started_at = now(), status = 'ok', "
                           "finished_at = now() WHERE id = :i"), {"i": run})  # fmt: skip
            assert migration.stages_in_flight(c) == 0
    finally:
        with session_scope() as c:
            c.execute(text("DELETE FROM pipeline_run WHERE id = :i"), {"i": run})


def test_a_migrator_that_died_does_not_hold_the_queue_for_ever(db_env) -> None:
    store = _Store()
    job = _queue(store)
    with session_scope() as c:  # the hold it took has run out
        migration.close_gate(c, reason="test", actor="test", hold_s=60)
        c.execute(text("UPDATE schema_gate SET expires_at = now() - interval '1 second' "
                       "WHERE id = 1"))  # fmt: skip
    assert not _closed()
    assert store.claim(job.id, "runner-A", LEASE) is not None


# ------------------------------------------------------------------ the window


def test_a_migration_with_a_job_in_flight_is_aborted_and_changes_nothing(test_cfg) -> None:
    store = _Store()
    job = _queue(store)
    claimed = store.claim(job.id, "runner-A", LEASE)
    assert claimed is not None
    before = migration.db_revision()

    with pytest.raises(migration.MigrationRefused, match="in flight"):
        migration.upgrade("t001", cfg=test_cfg, drain_s=1.0, actor="test")

    assert migration.db_revision() == before  # the schema was not touched
    assert not _closed()  # and the queue is not left held
    assert _column("state") and not _column("job_state")
    with session_scope() as c:
        run = c.execute(text("SELECT * FROM migration_run ORDER BY id DESC LIMIT 1")).mappings(
        ).first()  # fmt: skip
    assert run is not None
    assert (run["outcome"], run["drained"], run["in_flight_at_close"]) == ("aborted", False, 1)
    assert store.heartbeat("runner-A", LEASE) == 1  # the job in flight is still that runner's
    store.finish(claimed, "runner-A")


def test_only_one_migration_runs_at_a_time(test_cfg) -> None:
    with get_engine().connect() as held:
        held.execute(text("SELECT pg_advisory_lock(:k)"), {"k": migration.LOCK_KEY})
        held.commit()
        with pytest.raises(migration.MigrationRefused, match="schema lock"):
            migration.upgrade("t001", cfg=test_cfg, drain_s=0, actor="test")
        assert not _closed()  # the refused migrator did not touch the other one's gate
        held.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": migration.LOCK_KEY})
        held.commit()


def test_a_forward_incompatible_migration_applies_and_rolls_back_under_load(test_cfg) -> None:
    """The acceptance test. Three jobs wait in the queue and one is in flight. The migration
    closes the gate, waits for the job in flight to end, renames a column every worker's SQL uses
    and adds a NOT NULL one, then is rolled back - and the three waiting jobs each run once."""
    head = test_cfg.attributes["repo_head"]
    store = _Store()
    waiting = [_queue(store, f"rx-{i}.pdf") for i in range(3)]
    in_flight = _queue(store, "running.pdf")
    claimed = store.claim(in_flight.id, "runner-A", LEASE)
    assert claimed is not None

    out: dict[str, Any] = {}

    def migrate() -> None:
        try:
            out["rec"] = migration.upgrade("t001", cfg=test_cfg, drain_s=60, actor="test")
        except BaseException as exc:  # noqa: BLE001 - reported by the assertions below
            out["error"] = exc

    t = threading.Thread(target=migrate, name="migrator", daemon=True)
    t.start()
    assert _wait(_closed), "the gate was never closed"

    # it waits for the job in flight, and while it waits the queue is untouched
    assert migration.db_revision() == head
    for job in waiting:
        assert store.claim(job.id, "runner-B", LEASE) is None
    assert store.orphans(0.0, limit=20) == []
    with session_scope() as c:
        assert migration.queued(c) == 3
    assert t.is_alive()

    store.finish(claimed, "runner-A")  # the worker finishes its job: the drain ends
    t.join(90)
    assert not t.is_alive() and "error" not in out, out.get("error")
    rec = out["rec"]
    assert (rec["outcome"], rec["drained"], rec["in_flight_at_close"]) == ("applied", True, 1)
    assert rec["queued_at_close"] == 3
    assert migration.db_revision() == "t001"
    assert _column("job_state") and _column("lifecycle") and not _column("state")
    assert not _closed()  # the queue is released as soon as the schema is settled

    with session_scope() as c:  # the three waiting jobs are still waiting, none of them started
        rows = c.execute(text("SELECT id, attempts, finished_at FROM web_job "
                              "WHERE created_by = 'test-migration' AND id = ANY(:ids)"),
                         {"ids": [j.id for j in waiting]}).mappings().all()  # fmt: skip
    assert len(rows) == 3
    assert all(r["attempts"] == 0 and r["finished_at"] is None for r in rows)

    rolled = migration.downgrade(head, cfg=test_cfg, drain_s=30, actor="test")
    assert rolled["outcome"] == "rolled_back" and rolled["to_revision"] == head
    assert _column("state") and not _column("job_state") and not _column("lifecycle")
    assert not _closed()

    # after the rollback every waiting job runs, and each one exactly once
    ran: dict[str, int] = {}
    runner = JobRunner(_stages(ran), max_workers=2, store=_Store(), owner="runner-C")
    swept = store.orphans(0.0, limit=20)
    assert {j.id for j in waiting} <= set(swept)
    for job_id in [j.id for j in waiting]:
        assert runner.run_stored(job_id) == "review"
        assert runner.run_stored(job_id) == "not_claimed"  # a redelivered message is a no-op
    assert ran == {"rx-0.pdf": 1, "rx-1.pdf": 1, "rx-2.pdf": 1}
    with session_scope() as c:
        done = c.execute(text("SELECT state, attempts FROM web_job WHERE id = ANY(:ids)"),
                         {"ids": [j.id for j in waiting]}).all()  # fmt: skip
    assert sorted(done) == [("review", 1)] * 3


# ------------------------------------------------------------------ what it reports


def test_status_reports_the_schema_the_gate_and_the_queue(db_env, capsys) -> None:
    store = _Store()
    job = _queue(store)
    assert store.claim(job.id, "runner-A", LEASE) is not None
    _queue(store)

    st = migration.status()
    assert st["db_revision"] == st["code_revision"] and st["up_to_date"]
    assert st["gate_closed"] is False
    assert st["in_flight"] == 1 and st["queued"] >= 1

    assert migration.main(["status"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["db_revision"] == st["db_revision"] and printed["in_flight"] == 1
