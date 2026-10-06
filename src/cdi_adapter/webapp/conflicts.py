"""Multi-fact merge adjudication (backlog E5-S3; §5 S8, §4.1, migration 0027).

The S8 console (``webapp/review.py``) decides **one fact at a time**, which is the wrong shape for
a reconciliation conflict: when the S6 gate finds two facts of one patient disagreeing it writes a
``fact_conflict`` and holds *both*, and deciding them one by one neither shows the reviewer what
they are choosing between nor records *why* the loser lost. This module is the merge-specific side:

* ``GET /api/conflicts`` - the open conflicts, each with both facts in summary.
* ``GET /api/conflicts/{id}`` - the conflict with **both facts in full, each with its own
  evidence** (the same ``review.fact_detail`` payload the S8 console uses: page image, bbox, OCR
  span, code, confidence, findings, resolution trace), so the page can put them side by side.
* ``POST /api/conflicts/{id}/resolve`` ``{choice: keep_a|keep_b|merge, corrections?, note?}``.

**One decision, one record.** Whatever the choice, the resolution is written on the conflict row
itself - ``resolution``, ``result_fact_id``, ``resolved_by``, ``resolved_at``, ``note`` - plus a
single ``audit_log`` row against ``fact_conflict``. The facts move with it:

* ``keep_a`` / ``keep_b``: the kept fact becomes ``clinician_confirmed``, the other ``rejected``
  (``is_current = false``, ``clinical_status = 'entered-in-error'``), exactly as the S8 console
  would have set them;
* ``merge``: the reviewer's corrections are applied to the kept fact, which produces a new
  ``corrected`` fact that ``supersedes`` it (``review.apply_fact_correction``, the same path as an
  S8 correction, so medication detail and catalogue re-matching behave identically), and the other
  fact is rejected. ``merge`` therefore needs corrections - without them it is ``keep_a``.

Each fact that moved gets its reviewer provenance, its E4-S7 reviewer candidate and its E4-S1
decision record, as in the S8 console; then E5-S1 reconciliation and the E5-S2 summary run in the
same transaction. The reviewer is the signed-in user (E7-S1); a client-sent name is ignored.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from cdi_adapter import evidence, longitudinal, metrics, repo, summary
from cdi_adapter.db import session_scope
from cdi_adapter.webapp import auth
from cdi_adapter.webapp.review import (
    ReviewError,
    _close_tasks,
    _fact,
    _reviewer_provenance,
    apply_fact_correction,
    fact_detail,
)

router = APIRouter()

CHOICES = ("keep_a", "keep_b", "merge")


class ConflictResolution(BaseModel):
    choice: Literal["keep_a", "keep_b", "merge"]
    # merge only: which fact the merged one is a correction of (so it supersedes that one), and
    # the values it should carry (the same whitelist as an S8 correction)
    base: Literal["a", "b"] = "a"
    corrections: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=2000)


def _conflict(conn: Connection, conflict_id: UUID | str) -> dict[str, Any]:
    row = conn.execute(text("SELECT * FROM fact_conflict WHERE id = :i"),
                       {"i": str(conflict_id)}).mappings().first()  # fmt: skip
    if row is None:
        raise ReviewError(404, "conflict not found")
    return dict(row)


def _side(conn: Connection, fact_id: Any) -> dict[str, Any]:
    """One side of a conflict, in list form: enough to choose between them at a glance."""
    row = conn.execute(
        text("SELECT f.id, f.fact_type, f.local_text, f.code_system, f.code, f.code_display, "
             "  f.value_num, f.value_unit_ucum, f.value_text, f.review_state, f.is_current, "
             "  f.confidence_overall, e.period_start AS encounter_date, "
             "  f.source_doc_ids[1] AS document_id "
             "FROM clinical_fact f LEFT JOIN encounter e ON e.id = f.encounter_id "
             "WHERE f.id = :i"), {"i": str(fact_id)},
    ).mappings().first()  # fmt: skip
    return dict(row) if row else {"id": str(fact_id), "missing": True}


def open_conflicts(conn: Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Unresolved conflicts, worst first: a blocker is what holds a patient's record up."""
    rows = conn.execute(
        text("SELECT c.*, p.name AS patient_name, p.mpi_id FROM fact_conflict c "
             "LEFT JOIN patient_identity p ON p.id = c.patient_id "
             "WHERE NOT c.resolved ORDER BY CASE c.severity WHEN 'blocker' THEN 0 "
             "  WHEN 'warn' THEN 1 ELSE 2 END, c.created_at LIMIT :n"), {"n": limit},
    ).mappings().all()  # fmt: skip
    out = []
    for r in rows:
        c = dict(r)
        c["a"], c["b"] = _side(conn, c["fact_a"]), _side(conn, c["fact_b"])
        out.append(c)
    return out


def conflict_detail(conn: Connection, conflict_id: UUID | str) -> dict[str, Any]:
    """The conflict with both facts in full, each carrying its own evidence."""
    c = _conflict(conn, conflict_id)
    return {"conflict": c, "a": fact_detail(conn, c["fact_a"]), "b": fact_detail(conn, c["fact_b"])}


def _keep(conn: Connection, fact: dict[str, Any], reviewer: str) -> None:
    conn.execute(text("UPDATE clinical_fact SET review_state = 'clinician_confirmed', "
                      "updated_at = now() WHERE id = :i"), {"i": fact["id"]})  # fmt: skip


def _drop(conn: Connection, fact: dict[str, Any], reviewer: str) -> None:
    conn.execute(text("UPDATE clinical_fact SET review_state = 'rejected', is_current = false, "
                      "clinical_status = 'entered-in-error', updated_at = now() "
                      "WHERE id = :i"), {"i": fact["id"]})  # fmt: skip


def resolve(conn: Connection, conflict_id: UUID | str, d: ConflictResolution,
            reviewer: str) -> dict[str, Any]:  # fmt: skip
    """Settle one conflict as a single auditable decision. See the module docstring."""
    c = _conflict(conn, conflict_id)
    if c["resolved"]:
        raise ReviewError(409, f"conflict already resolved ({c['resolution']}) "
                               f"by {c['resolved_by']}")  # fmt: skip
    if d.choice == "merge" and not d.corrections:
        raise ReviewError(422, "merge needs the values the merged fact should carry; "
                               "with none, the decision is keep_a")  # fmt: skip

    a, b = _fact(conn, c["fact_a"]), _fact(conn, c["fact_b"])
    keep_a = d.choice == "keep_a" or (d.choice == "merge" and d.base == "a")
    kept, dropped = (a, b) if keep_a else (b, a)
    for f in (kept, dropped):
        if not f["is_current"]:
            raise ReviewError(409, f"fact {f['id']} is no longer current; it was already decided")

    result_id = kept["id"]
    if d.choice == "merge":
        result_id = apply_fact_correction(conn, kept, d.corrections or {}, reviewer)
        if kept["fact_type"] == "medication":  # the merged name is identified in the catalogue
            from cdi_adapter.catalogue.service import rematch_fact

            rematch_fact(conn, result_id)
    else:
        _keep(conn, kept, reviewer)
    _drop(conn, dropped, reviewer)

    action = "correct" if d.choice == "merge" else "accept"
    _reviewer_provenance(conn, result_id, kept, reviewer)
    _reviewer_provenance(conn, dropped["id"], dropped, reviewer)
    evidence.record_reviewer_decision(conn, result_id, action, reviewer, kept["id"])
    evidence.record_reviewer_decision(conn, dropped["id"], "reject", reviewer, dropped["id"])
    from cdi_adapter.training.calibration import record_decision

    record_decision(conn, result_id)
    record_decision(conn, dropped["id"])

    conn.execute(
        text("UPDATE fact_conflict SET resolved = true, resolution = :r, result_fact_id = :f, "
             "resolved_by = :u, resolved_at = now(), note = :n WHERE id = :i"),
        {"r": d.choice, "f": str(result_id) if d.choice == "merge" else None, "u": reviewer,
         "n": d.note, "i": c["id"]},
    )  # fmt: skip
    detail = {"resolution": d.choice, "kept_fact": str(kept["id"]),
              "rejected_fact": str(dropped["id"]), "result_fact": str(result_id),
              "conflict_type": c["conflict_type"], "severity": c["severity"],
              "corrections": d.corrections, "note": d.note}  # fmt: skip
    repo.write_audit(conn, actor=reviewer, action="update", entity="fact_conflict",
                     entity_id=str(c["id"]), detail=detail)  # fmt: skip
    closed = _close_tasks(conn, [str(a["id"]), str(b["id"]), str(result_id)])
    # the record moved: E5-S1 continuity and the E5-S2 summary follow, in this transaction
    longitudinal.reconcile_patient(conn, c["patient_id"], actor=reviewer)
    summary.refresh_patient_summary(conn, c["patient_id"])
    metrics.CONFLICTS_RESOLVED.labels(d.choice).inc()
    return {"conflict_id": str(c["id"]), "resolution": d.choice, "kept_fact_id": str(kept["id"]),
            "rejected_fact_id": str(dropped["id"]), "result_fact_id": str(result_id),
            "tasks_closed": closed}  # fmt: skip


def _http(fn):  # noqa: ANN001, ANN202 - the same translation the S8 routes use
    def wrapped(*a: Any, **kw: Any) -> Any:
        try:
            return fn(*a, **kw)
        except ReviewError as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    return wrapped


@router.get("/api/conflicts")
def api_conflicts() -> list[dict[str, Any]]:
    with session_scope() as c:
        return open_conflicts(c)


@router.get("/api/conflicts/{conflict_id}")
def api_conflict(conflict_id: UUID) -> dict[str, Any]:
    with session_scope() as c:
        return _http(conflict_detail)(c, conflict_id)


@router.post("/api/conflicts/{conflict_id}/resolve")
def api_resolve(conflict_id: UUID, body: ConflictResolution, request: Request) -> dict[str, Any]:
    reviewer = auth.actor(request).username
    with session_scope() as c:
        return _http(resolve)(c, conflict_id, body, reviewer)
