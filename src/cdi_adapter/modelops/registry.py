"""Registered model versions and the rollout log (backlog E10-S5, migration 0006).

A *version* names exactly what a gateway loads: model id + pinned revision + backend (+
quantization). A *rollout state* says which version serves as ``primary`` (the incumbent) at which
gateway URL, and optionally which ``canary`` version gets ``canary_percent`` % of documents. Every
change appends a new state row; the latest row is live. Nothing is ever rewritten, so "the previous
version" is always answerable from the log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

ACTIONS = ("deploy", "canary", "set_percent", "promote", "rollback")


class RolloutError(Exception):
    """An operator command that cannot be carried out; nothing was changed."""


@dataclass(frozen=True)
class RolloutState:
    id: int
    action: str
    primary_version: str
    primary_url: str
    canary_version: str | None = None
    canary_url: str | None = None
    canary_percent: int = 0
    actor: str = ""
    reason: str | None = None

    @property
    def has_canary(self) -> bool:
        return self.canary_version is not None and self.canary_percent > 0


def register(conn: Connection, *, version: str, model_id: str, revision: str, backend: str,
             registered_by: str, quantization: str | None = None,
             eval_report: dict[str, Any] | None = None,
             notes: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Register a version once. A version is immutable: re-registering the same name with a
    different model is refused (a changed model must get a new version name)."""
    existing = get_version(conn, version)
    if existing is not None:
        had = (existing["model_id"], existing["revision"], existing["backend"],
               existing["quantization"])  # fmt: skip
        same = had == (model_id, revision, backend, quantization)
        if not same:
            raise RolloutError(f"version {version} is already registered with another model; "
                               "register the change under a new version name")  # fmt: skip
        return existing

    try:
        conn.execute(
            text("INSERT INTO model_version (version, model_id, revision, backend, quantization, "
                 "eval_report, notes, registered_by) VALUES (:v, :m, :r, :b, :q, "
                 "CAST(:e AS jsonb), :n, :by)"),
            {"v": version, "m": model_id, "r": revision, "b": backend, "q": quantization,
             "e": json.dumps(eval_report) if eval_report is not None else None, "n": notes,
             "by": registered_by},
        )  # fmt: skip
    except IntegrityError as exc:
        raise RolloutError(f"cannot register {version}: {exc.orig}") from exc
    found = get_version(conn, version)
    assert found is not None
    return found


def set_eval_report(conn: Connection, version: str, report: dict[str, Any]) -> None:
    """Attach the E2-S7 offline eval report (gold-set metrics) to a registered version."""
    n = conn.execute(text("UPDATE model_version SET eval_report = CAST(:e AS jsonb) "
                          "WHERE version = :v"),
                     {"e": json.dumps(report), "v": version}).rowcount  # fmt: skip
    if n != 1:
        raise RolloutError(f"version {version} is not registered")


def get_version(conn: Connection, version: str) -> dict[str, Any] | None:
    row = conn.execute(text("SELECT * FROM model_version WHERE version = :v"),
                       {"v": version}).mappings().first()  # fmt: skip
    return dict(row) if row else None


def list_versions(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(text("SELECT * FROM model_version ORDER BY registered_at, version"))
    return [dict(r) for r in rows.mappings()]


def _state(row: Any) -> RolloutState:
    return RolloutState(id=row["id"], action=row["action"], primary_version=row["primary_version"],
                        primary_url=row["primary_url"], canary_version=row["canary_version"],
                        canary_url=row["canary_url"], canary_percent=row["canary_percent"],
                        actor=row["actor"], reason=row["reason"])  # fmt: skip


def current(conn: Connection) -> RolloutState | None:
    row = conn.execute(text("SELECT * FROM model_rollout ORDER BY id DESC LIMIT 1")).mappings()
    first = row.first()
    return _state(first) if first else None


def history(conn: Connection, limit: int = 50) -> list[RolloutState]:
    rows = conn.execute(text("SELECT * FROM model_rollout ORDER BY id DESC LIMIT :n"), {"n": limit})
    return [_state(r) for r in rows.mappings()]


def previous_primary(conn: Connection) -> RolloutState | None:
    """The last state whose primary differs from today's: what a rollback returns to."""
    now = current(conn)
    if now is None:
        return None
    row = conn.execute(
        text("SELECT * FROM model_rollout WHERE id < :id AND primary_version <> :v "
             "ORDER BY id DESC LIMIT 1"), {"id": now.id, "v": now.primary_version},
    ).mappings().first()  # fmt: skip
    return _state(row) if row else None


def canary_started_at(conn: Connection) -> Any:
    """When the live canary version started taking traffic (for the live comparison window)."""
    now = current(conn)
    if now is None or now.canary_version is None:
        return None
    return conn.execute(
        text("SELECT min(at) FROM model_rollout r WHERE r.canary_version = :v AND r.id > "
             "coalesce((SELECT max(id) FROM model_rollout WHERE id <= :id AND "
             "canary_version IS DISTINCT FROM :v), 0)"), {"v": now.canary_version, "id": now.id},
    ).scalar_one()  # fmt: skip


def lock(conn: Connection) -> None:
    """One writer at a time: two operators cannot both build on the same "current" state. Taken
    at the start of a command's transaction, before it reads the current state."""
    conn.execute(text("LOCK TABLE model_rollout IN SHARE ROW EXCLUSIVE MODE"))


def _urls(url: str) -> str:
    """One gateway or several replicas (E10-S9), normalised: no trailing slash, no spaces."""
    return ",".join(u.strip().rstrip("/") for u in url.split(",") if u.strip())


def append(conn: Connection, *, actor: str, action: str, primary_version: str, primary_url: str,
           canary_version: str | None = None, canary_url: str | None = None,
           canary_percent: int = 0, reason: str | None = None) -> RolloutState:  # fmt: skip
    if action not in ACTIONS:
        raise RolloutError(f"unknown action {action}")
    for v in (primary_version, canary_version):
        if v is not None and get_version(conn, v) is None:
            raise RolloutError(f"version {v} is not registered")
    if canary_version is not None and canary_version == primary_version:
        raise RolloutError("the canary must be a different version than the primary")
    if not 0 <= canary_percent <= 100:
        raise RolloutError("canary percent must be 0-100")
    lock(conn)
    row = conn.execute(
        text("INSERT INTO model_rollout (actor, action, primary_version, primary_url, "
             "canary_version, canary_url, canary_percent, reason) VALUES (:a, :act, :pv, :pu, "
             ":cv, :cu, :cp, :r) RETURNING *"),
        {"a": actor, "act": action, "pv": primary_version, "pu": _urls(primary_url),
         "cv": canary_version, "cu": _urls(canary_url) if canary_url else None,
         "cp": canary_percent if canary_version else 0, "r": reason},
    ).mappings().one()  # fmt: skip
    return _state(row)
