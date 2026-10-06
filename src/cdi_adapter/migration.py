"""Schema changes with workers in flight (backlog E9-S1; migration 0021, docs/ha.md).

A forward-incompatible migration - a renamed or dropped column, a new constraint - breaks the SQL
of every worker already running, and the cluster is not stopped for it: uploads keep arriving and
``web_job`` keeps rows that no runner has finished. This module is how a migration is applied, and
rolled back, without losing a queued job or running one twice::

    python -m cdi_adapter.migration status
    python -m cdi_adapter.migration upgrade head
    python -m cdi_adapter.migration downgrade 0020

:func:`window` is the whole of it, and both commands go through it:

1. **One migrator**: a PostgreSQL advisory lock, held for the window. A second one refuses.
2. **Close the gate** (``schema_gate``, with ``expires_at``): from here no runner claims a *new*
   job. The claim and the orphan sweep in ``webapp/jobstore.py`` both refuse while it is closed,
   so the queue holds still instead of being run against a schema the code does not match.
   Running jobs are **not** interrupted and keep their lease (``heartbeat`` still renews).
3. **Drain**: wait until nothing is in flight - no web job with a live lease, and no pipeline
   stage still running (the folder-watch path holds no lease, so its open ``pipeline_run`` stands
   for a worker mid-document) - up to ``drain_s``. Nothing is applied while a worker is mid-job:
   if it does not drain, the migration is *aborted* - the gate opens again and the schema is
   untouched (``force=True`` applies it anyway, for a cluster that is already stopped).
4. **Run Alembic**, then open the gate in ``finally`` - the queued jobs are claimed again by the
   next sweep, each exactly once, because the claim is still the only thing that decides who runs
   a job. A failed or rolled-back migration opens the gate too; it is never left closed.

Every window writes one ``migration_run`` row (the revisions, what was in flight, whether it
drained, the outcome), so what happened to the queue during a migration is on the record.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.config import get_settings
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.logging import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
LOCK_KEY = 912_300_021  # pg_advisory_lock: one migrator at a time


class MigrationRefused(Exception):
    """The migration was not applied, and the schema was not touched."""


# --------------------------------------------------------------------------- the gate


def gate(conn: Connection) -> dict[str, Any]:
    """The gate row, as it is."""
    row = conn.execute(text("SELECT * FROM schema_gate WHERE id = 1")).mappings().first()
    return dict(row) if row else {"closed": False}


def is_closed(conn: Connection) -> bool:
    """True while the gate holds the queue (an expired hold does not)."""
    return bool(conn.execute(text(
        "SELECT EXISTS (SELECT 1 FROM schema_gate WHERE closed "
        "AND (expires_at IS NULL OR expires_at > now()))")).scalar())  # fmt: skip


def jobs_in_flight(conn: Connection) -> int:
    """Web-app jobs a runner is working on right now (unfinished, with a live lease)."""
    return int(conn.execute(text(
        "SELECT count(*) FROM web_job WHERE finished_at IS NULL AND lease_until > now()"
    )).scalar_one())  # fmt: skip


def stages_in_flight(conn: Connection) -> int:
    """Pipeline stages still running. The folder-watch / ingest-API path (the ``cdi`` queue) holds
    no lease, so its open ``pipeline_run`` stands for a worker mid-document. A ``running`` row a
    dead worker left behind would hold every migration for ever, so one older than
    ``CDI_MIGRATION_STAGE_STALE_S`` is not counted."""
    return int(conn.execute(text(
        "SELECT count(*) FROM pipeline_run WHERE status = 'running' AND finished_at IS NULL "
        "AND started_at > now() - make_interval(secs => :s)"),
        {"s": get_settings().migration_stage_stale_s}).scalar_one())  # fmt: skip


def in_flight(conn: Connection) -> int:
    """Everything a worker is in the middle of, on either path: what a migration waits for."""
    return jobs_in_flight(conn) + stages_in_flight(conn)


def queued(conn: Connection) -> int:
    """Jobs that are accepted and unfinished with nobody on them: the queue to protect."""
    return int(conn.execute(text(
        "SELECT count(*) FROM web_job WHERE finished_at IS NULL "
        "AND (lease_until IS NULL OR lease_until <= now())"
    )).scalar_one())  # fmt: skip


def close_gate(conn: Connection, *, reason: str, actor: str, hold_s: float,
               from_revision: str | None = None,
               to_revision: str | None = None) -> None:  # fmt: skip
    conn.execute(text(
        "UPDATE schema_gate SET closed = true, reason = :r, actor = :a, from_revision = :f, "
        "to_revision = :t, closed_at = now(), expires_at = now() + make_interval(secs => :h), "
        "updated_at = now() WHERE id = 1"),
        {"r": reason, "a": actor, "f": from_revision, "t": to_revision, "h": hold_s})  # fmt: skip
    detail = {"closed": True, "reason": reason, "hold_s": hold_s,
              "from_revision": from_revision, "to_revision": to_revision}  # fmt: skip
    repo.write_audit(conn, actor=actor, action="update", entity="schema_gate", entity_id="1",
                     detail=detail)  # fmt: skip


def open_gate(conn: Connection, *, actor: str) -> None:
    conn.execute(text(
        "UPDATE schema_gate SET closed = false, reason = NULL, expires_at = NULL, "
        "updated_at = now() WHERE id = 1"))  # fmt: skip
    repo.write_audit(conn, actor=actor, action="update", entity="schema_gate", entity_id="1",
                     detail={"closed": False})  # fmt: skip


def _open_gate_safely(actor: str) -> None:
    """Open the gate, come what may. A downgrade past 0021 takes ``schema_gate`` with it, and a
    gate that is gone holds nothing - so a missing table is not an error here."""
    try:
        with session_scope() as c:
            if c.execute(text("SELECT to_regclass('schema_gate')")).scalar() is None:
                return
            open_gate(c, actor=actor)
    except Exception as exc:  # noqa: BLE001 - never leave the window with an exception of its own
        log.error("schema_gate_not_opened", error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- Alembic


def _cfg() -> Any:
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "db" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def db_revision() -> str | None:
    """The revision the database is at."""
    with session_scope() as c:
        if c.execute(text("SELECT to_regclass('alembic_version')")).scalar() is None:
            return None
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def script_head(cfg: Any = None) -> str | None:
    """The head revision of the migrations shipped with this code."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(cfg or _cfg()).get_current_head()


# --------------------------------------------------------------------------- the window


@contextmanager
def window(direction: str, target: str, *, actor: str | None = None,
           drain_s: float | None = None, hold_s: float | None = None,
           force: bool = False) -> Iterator[dict[str, Any]]:  # fmt: skip
    """Hold the queue for one schema change. See the module docstring for the four steps.

    Yields the record of this window (its ``migration_run`` id, the revisions, the drain), and
    sets ``outcome`` on the row as it ends. Raises :class:`MigrationRefused` without touching the
    schema when another migrator holds the lock, or when the jobs in flight do not drain.
    """
    s = get_settings()
    actor = actor or f"migration:{os.environ.get('USERNAME') or os.environ.get('USER') or 'cli'}"
    drain_s = s.migration_drain_s if drain_s is None else drain_s
    hold_s = s.migration_hold_s if hold_s is None else hold_s
    start = db_revision()
    rec: dict[str, Any] = {"direction": direction, "target": target, "from_revision": start,
                           "to_revision": None, "drained": False, "outcome": "running"}  # fmt: skip

    lock = get_engine().connect()  # the lock lives as long as this connection
    ours = False  # only the migrator that closed the gate opens it again
    locked = False
    try:
        if not lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            raise MigrationRefused("another migration holds the schema lock")
        locked = True
        lock.commit()  # the lock is the session's, not the transaction's: hold no snapshot
        with session_scope() as c:
            flight, waiting = in_flight(c), queued(c)
            close_gate(c, reason=f"{direction} -> {target}", actor=actor, hold_s=hold_s,
                       from_revision=start, to_revision=target)  # fmt: skip
            ours = True
            rec["id"] = c.execute(text(
                "INSERT INTO migration_run (direction, target, from_revision, actor, "
                "in_flight_at_close, queued_at_close) VALUES (:d, :t, :f, :a, :n, :q) "
                "RETURNING id"),
                {"d": direction, "t": target, "f": start, "a": actor, "n": flight,
                 "q": waiting}).scalar_one()  # fmt: skip
        rec["in_flight_at_close"], rec["queued_at_close"] = flight, waiting
        log.info("schema_gate_closed", direction=direction, target=target, from_revision=start,
                 in_flight=flight, queued=waiting, drain_s=drain_s, hold_s=hold_s)  # fmt: skip

        t0 = time.monotonic()
        left = _drain(drain_s)
        rec["drain_seconds"] = round(time.monotonic() - t0, 3)
        rec["drained"] = left == 0
        if left and not force:
            _finish_run(rec, "aborted", f"{left} job(s) still in flight after {drain_s}s")
            raise MigrationRefused(
                f"{left} job(s) in flight after {drain_s}s: nothing applied. Wait for them, "
                f"raise CDI_MIGRATION_DRAIN_S, or pass --force once the workers are stopped."
            )
        if left:
            log.warning("schema_migration_forced", in_flight=left)
        _mark_drain(rec)

        try:
            yield rec
        except Exception as exc:
            _finish_run(rec, "failed", f"{type(exc).__name__}: {exc}")
            log.error("schema_migration_failed", direction=direction, target=target,
                      error=f"{type(exc).__name__}: {exc}")  # fmt: skip
            raise
        rec["to_revision"] = db_revision()
        _finish_run(rec, "rolled_back" if direction == "downgrade" else "applied")
        log.info("schema_migration_done", direction=direction,
                 from_revision=start, to_revision=rec["to_revision"])  # fmt: skip
    finally:
        if ours:
            _open_gate_safely(actor)
        try:
            if locked:
                lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
                lock.commit()
        except Exception:  # noqa: BLE001 - closing the connection releases it anyway
            pass
        lock.close()


def _drain(drain_s: float, poll: float = 0.5) -> int:
    """Wait for the jobs in flight to end; returns how many are left."""
    deadline = time.monotonic() + drain_s
    while True:
        with session_scope() as c:
            left = in_flight(c)
        if left == 0 or time.monotonic() >= deadline:
            return left
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))


def _mark_drain(rec: dict[str, Any]) -> None:
    if "id" not in rec:
        return
    with session_scope() as c:
        c.execute(text("UPDATE migration_run SET drained = :d, drain_seconds = :s WHERE id = :i"),
                  {"d": rec["drained"], "s": rec.get("drain_seconds"), "i": rec["id"]})  # fmt: skip


def _finish_run(rec: dict[str, Any], outcome: str, detail: str | None = None) -> None:
    rec["outcome"], rec["detail"] = outcome, detail
    if "id" not in rec:
        return
    try:
        with session_scope() as c:
            c.execute(text(
                "UPDATE migration_run SET outcome = :o, detail = :x, to_revision = :t, "
                "drained = :d, drain_seconds = :s, finished_at = now() WHERE id = :i"),
                {"o": outcome, "x": detail, "t": rec.get("to_revision"), "d": rec["drained"],
                 "s": rec.get("drain_seconds"), "i": rec["id"]})  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - the schema may be mid-change; the log has it
        log.error("migration_run_not_recorded", outcome=outcome, error=str(exc))


# --------------------------------------------------------------------------- commands


def upgrade(target: str = "head", **kw: Any) -> dict[str, Any]:
    """Apply migrations up to ``target`` with the queue held (see :func:`window`)."""
    from alembic import command

    cfg = kw.pop("cfg", None) or _cfg()
    with window("upgrade", target, **kw) as rec:
        command.upgrade(cfg, target)
    return rec


def downgrade(target: str, **kw: Any) -> dict[str, Any]:
    """Roll the schema back to ``target`` with the queue held (see :func:`window`)."""
    from alembic import command

    cfg = kw.pop("cfg", None) or _cfg()
    with window("downgrade", target, **kw) as rec:
        command.downgrade(cfg, target)
    return rec


def status() -> dict[str, Any]:
    """Where the schema is, whether the queue is held, and what is waiting on it."""
    with session_scope() as c:
        g = gate(c)
        out = {"db_revision": db_revision(), "code_revision": script_head(),
               "gate_closed": is_closed(c), "gate_reason": g.get("reason"),
               "gate_actor": g.get("actor"), "gate_expires_at": g.get("expires_at"),
               "in_flight": in_flight(c), "jobs_in_flight": jobs_in_flight(c),
               "stages_in_flight": stages_in_flight(c), "queued": queued(c)}  # fmt: skip
        out["up_to_date"] = out["db_revision"] == out["code_revision"]
        out["last_runs"] = [dict(r) for r in c.execute(text(
            "SELECT direction, target, from_revision, to_revision, drained, outcome, detail, "
            "started_at FROM migration_run ORDER BY id DESC LIMIT 5")).mappings()]  # fmt: skip
    return out


def main(argv: list[str] | None = None) -> int:
    import json

    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    cmd = args[0] if args else "status"
    try:
        if cmd == "status":
            print(json.dumps(status(), indent=2, default=str))
        elif cmd == "upgrade":
            print(json.dumps(upgrade(args[1] if len(args) > 1 else "head", force=force),
                             indent=2, default=str))  # fmt: skip
        elif cmd == "downgrade":
            if len(args) < 2:
                print("downgrade needs a target revision, e.g. 0020", file=sys.stderr)
                return 2
            print(json.dumps(downgrade(args[1], force=force), indent=2, default=str))
        elif cmd == "open-gate":  # the escape hatch: a migrator that died mid-window
            _open_gate_safely("migration:open-gate")
            print(json.dumps(status(), indent=2, default=str))
        else:
            print(__doc__)
            return 2
    except MigrationRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI
    raise SystemExit(main())
