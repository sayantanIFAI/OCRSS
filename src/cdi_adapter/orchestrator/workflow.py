"""Document workflow: every transition persisted, and replayable (backlog E12-S1).

Every stage of the pipeline opens and closes a ``pipeline_run``; when one finishes, the database
appends a ``workflow_step`` (migration 0019) - so a document's whole path, from both the web job
and the folder-watch (Celery) path, is recorded without any stage code knowing about it.

``replay_plan`` turns a document's recorded steps back into calls: the successful steps after its
latest ingest, in the order they ran, each with the arguments that reproduce it (an OCR step
re-runs with the engine it used). ``replay`` executes the plan with the same stage functions the
pipeline uses, records a ``workflow_replay`` row, and the replayed stages append their own steps.
Ingest is never replayed: it needs the original upload, which the replay does not re-send.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter.db import session_scope

STAGES = ("ingest", "ocr", "classify", "extract", "terminology", "validate", "project", "agent")


def steps(conn: Connection, document_id: UUID | str) -> list[dict[str, Any]]:
    """A document's recorded transitions, in the order they happened."""
    return [dict(r) for r in conn.execute(
        text("SELECT * FROM workflow_step WHERE document_id = :d ORDER BY id"),
        {"d": str(document_id)},
    ).mappings()]  # fmt: skip


def _args(step: dict[str, Any]) -> dict[str, Any]:
    if step["stage"] == "ocr" and (step.get("outputs") or {}).get("engine"):
        return {"force_engine": step["outputs"]["engine"]}
    return {}


def replay_plan(conn: Connection, document_id: UUID | str,
                from_stage: str | None = None) -> list[dict[str, Any]]:  # fmt: skip
    if from_stage is not None and from_stage not in STAGES:
        raise ValueError(f"from_stage must be one of {STAGES}")
    rec = steps(conn, document_id)
    last_ingest = max((i for i, s in enumerate(rec) if s["stage"] == "ingest"), default=-1)
    plan = []
    for s in rec[last_ingest + 1 :]:
        if s["status"] != "ok":
            continue
        plan.append({"stage": s["stage"], "args": _args(s), "step_id": s["id"]})
    if from_stage is not None:
        first = next((i for i, p in enumerate(plan) if p["stage"] == from_stage), None)
        plan = [] if first is None else plan[first:]
    return plan


def _runners() -> dict[str, Callable[..., Any]]:
    from cdi_adapter.agent.medication import run_medication_agent
    from cdi_adapter.classify.service import classify_document
    from cdi_adapter.extract.service import extract_document
    from cdi_adapter.fhir.service import project_document
    from cdi_adapter.ocr.service import ocr_document
    from cdi_adapter.terminology.service import bind_document
    from cdi_adapter.validate.service import validate_document

    return {"ocr": ocr_document, "classify": classify_document, "extract": extract_document,
            "terminology": bind_document, "validate": validate_document,
            "project": project_document, "agent": run_medication_agent}  # fmt: skip


def replay(document_id: UUID | str, *, from_stage: str | None = None, actor: str = "replay",
           runners: dict[str, Callable[..., Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """Re-run a document's recorded steps; stops at the first failure."""
    started = datetime.now(UTC)
    with session_scope() as c:
        plan = replay_plan(c, document_id, from_stage)
    run = runners or _runners()
    results: list[dict[str, Any]] = []
    status = "ok"
    for p in plan:
        try:
            out = run[p["stage"]](str(document_id), **p["args"])
            results.append({"stage": p["stage"], "status": "ok",
                            "result": json.loads(json.dumps(out, default=str))})  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - recorded, and the replay stops there
            results.append({"stage": p["stage"], "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"[:500]})  # fmt: skip
            status = "failed"
            break
    with session_scope() as c:
        rid = c.execute(
            text("INSERT INTO workflow_replay (document_id, from_stage, plan, results, status, "
                 "  requested_by, started_at) VALUES (:d, :f, CAST(:p AS jsonb), "
                 "  CAST(:r AS jsonb), :s, :by, :at) RETURNING id"),
            {"d": str(document_id), "f": from_stage, "p": json.dumps(plan, default=str),
             "r": json.dumps(results, default=str), "s": status, "by": actor, "at": started},
        ).scalar_one()  # fmt: skip
    return {"replay_id": str(rid), "status": status, "plan": plan, "results": results}
