"""S8 human adjudication (ARCHITECTURE §5 S8, §7.1).

* ``GET /api/review/tasks`` — open ``review_task``s with their held facts.
* ``GET /api/review/facts/{fact_id}`` — the fact, its ``medication_detail``, every
  ``fact_provenance`` row -> resolved ``ocr_block`` texts + the ``bbox_union`` + the page image url
  (``/api/documents/{id}/pages/{n}``) + the rule findings.
* ``POST /api/facts/{fact_id}/review`` ``{action: accept|correct|reject, corrections?, reviewer?,
  note?}``: accept -> ``clinician_confirmed``; correct -> ``apply_fact_correction`` (whitelisted
  fields) -> a new current fact ``corrected`` that ``supersedes`` the original; reject ->
  ``rejected``, ``is_current=false``, ``clinical_status='entered-in-error'``.

Every decision writes a ``fact_provenance`` row with ``agent = <reviewer>`` and
``model_stack = {"reviewer": "human"}``, an ``audit_log`` row, and closes the ``review_task`` once
none of its facts remain ``in_review``. ``GET /api/jobs/{id}/fhir`` then re-projects live.
(The reviewer identity comes from the request body until backlog E7-S1 adds sessions.)
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.db import session_scope

router = APIRouter()

CORRECTABLE = {
    "local_text", "code", "code_system", "code_display", "value_num", "value_unit_ucum",
    "value_text", "clinical_status", "ref_range_low", "ref_range_high", "ref_range_text",
    "abnormal_flag",
}  # fmt: skip
REVIEWABLE = ("pending", "in_review", "auto_accepted")
_COPY_COLS = (
    "patient_id", "encounter_id", "fact_type", "code_system", "code", "code_display",
    "code_status", "local_text", "value_kind", "value_num", "value_unit_ucum", "value_code_system",
    "value_code", "value_code_display", "value_text", "value_bool", "value_low_num",
    "value_high_num", "ref_range_low", "ref_range_high", "ref_range_text", "abnormal_flag",
    "clinical_status", "verification", "onset", "effective_time", "asserted_time",
    "extraction_id", "source_doc_ids", "confidence_overall", "confidence_ocr",
    "confidence_extract", "confidence_terminology", "dedup_key",
)  # fmt: skip


class ReviewDecision(BaseModel):
    action: Literal["accept", "correct", "reject"]
    corrections: dict[str, Any] | None = None
    reviewer: str = Field(default="reviewer", min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=2000)


class ReviewError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _fact(conn: Connection, fact_id: UUID | str) -> dict[str, Any]:
    row = conn.execute(text("SELECT * FROM clinical_fact WHERE id = :i"),
                       {"i": str(fact_id)}).mappings().first()  # fmt: skip
    if row is None:
        raise ReviewError(404, "fact not found")
    return dict(row)


def _reviewer_provenance(conn, fact_id, prior: dict[str, Any], reviewer: str) -> None:
    src = repo.get_provenance(conn, prior["id"])
    first = src[0] if src else {}
    blocks = [str(b) for b in first.get("ocr_block_ids") or []]
    repo.insert_fact_provenance(
        conn, fact_id=fact_id, source_doc_id=first.get("source_doc_id"),
        page_id=first.get("page_id"), ocr_block_ids=blocks,
        bbox_union=first.get("bbox_union"), extracted_text=first.get("extracted_text"),
        pipeline_run_ids=[], model_stack={"reviewer": "human"}, agent=reviewer,
    )  # fmt: skip


def apply_fact_correction(conn: Connection, fact: dict[str, Any], corrections: dict[str, Any],
                          reviewer: str) -> UUID:  # fmt: skip
    bad = set(corrections) - CORRECTABLE
    if bad:
        raise ReviewError(422, f"fields not correctable: {sorted(bad)}")
    if not corrections:
        raise ReviewError(422, "correct needs at least one field in corrections")
    new = {c: fact[c] for c in _COPY_COLS}
    new.update(corrections)
    if "code" in corrections:
        new["code_status"] = "bound" if corrections.get("code") else "local_only"
    cols = [*new, "review_state", "supersedes", "is_current"]
    vals = {**new, "review_state": "corrected", "supersedes": fact["id"], "is_current": True}
    new_id = conn.execute(
        text(f"INSERT INTO clinical_fact ({', '.join(cols)}) "
             f"VALUES ({', '.join(':' + c for c in cols)}) RETURNING id"), vals
    ).scalar_one()  # fmt: skip
    med = conn.execute(text("SELECT * FROM medication_detail WHERE fact_id = :i"),
                       {"i": fact["id"]}).mappings().first()  # fmt: skip
    if med:
        repo.insert_medication_detail(conn, new_id, **{k: med[k] for k in repo.MED_COLUMNS})
    conn.execute(text("UPDATE clinical_fact SET is_current = false, review_state = 'corrected', "
                      "updated_at = now() WHERE id = :i"), {"i": fact["id"]})  # fmt: skip
    for p in repo.get_provenance(conn, fact["id"]):
        repo.insert_fact_provenance(
            conn, fact_id=new_id, source_doc_id=p["source_doc_id"], page_id=p["page_id"],
            ocr_block_ids=[str(b) for b in p["ocr_block_ids"] or []], bbox_union=p["bbox_union"],
            extracted_text=p["extracted_text"],
            pipeline_run_ids=[str(r) for r in p["pipeline_run_ids"] or []],
            model_stack=p["model_stack"], agent=p["agent"],
        )  # fmt: skip
    return new_id


def _close_tasks(conn: Connection, fact_ids: list[str]) -> int:
    rows = conn.execute(
        text("SELECT id, fact_ids FROM review_task WHERE status = 'open' "
             "AND fact_ids && CAST(:ids AS uuid[])"), {"ids": fact_ids}
    ).mappings().all()  # fmt: skip
    closed = 0
    for t in rows:
        still = conn.execute(
            text("SELECT count(*) FROM clinical_fact WHERE id = ANY(:ids) "
                 "AND review_state IN ('pending', 'in_review') AND is_current"),
            {"ids": list(t["fact_ids"])},
        ).scalar_one()  # fmt: skip
        if still == 0:
            conn.execute(text("UPDATE review_task SET status = 'closed', closed_at = now() "
                              "WHERE id = :i"), {"i": t["id"]})  # fmt: skip
            closed += 1
    return closed


def decide(conn: Connection, fact_id: UUID | str, d: ReviewDecision) -> dict[str, Any]:
    fact = _fact(conn, fact_id)
    if not fact["is_current"] or fact["review_state"] not in REVIEWABLE:
        raise ReviewError(409, f"fact is {fact['review_state']}; already adjudicated")
    result_id = fact["id"]
    if d.action == "accept":
        conn.execute(text("UPDATE clinical_fact SET review_state = 'clinician_confirmed', "
                          "updated_at = now() WHERE id = :i"), {"i": fact["id"]})  # fmt: skip
        state = "clinician_confirmed"
    elif d.action == "reject":
        conn.execute(text("UPDATE clinical_fact SET review_state = 'rejected', is_current = false, "
                          "clinical_status = 'entered-in-error', updated_at = now() "
                          "WHERE id = :i"), {"i": fact["id"]})  # fmt: skip
        state = "rejected"
    else:
        result_id = apply_fact_correction(conn, fact, d.corrections or {}, d.reviewer)
        state = "corrected"
    _reviewer_provenance(conn, result_id, fact, d.reviewer)
    repo.write_audit(conn, actor=d.reviewer, action="update", entity="clinical_fact",
                     entity_id=fact["id"],
                     detail={"action": d.action, "result_fact": str(result_id),
                             "corrections": d.corrections, "note": d.note})  # fmt: skip
    closed = _close_tasks(conn, [str(fact["id"]), str(result_id)])
    return {"fact_id": str(fact["id"]), "result_fact_id": str(result_id), "review_state": state,
            "tasks_closed": closed}  # fmt: skip


def fact_detail(conn: Connection, fact_id: UUID | str) -> dict[str, Any]:
    fact = _fact(conn, fact_id)
    med = conn.execute(text("SELECT * FROM medication_detail WHERE fact_id = :i"),
                       {"i": fact["id"]}).mappings().first()  # fmt: skip
    prov = []
    for p in repo.get_provenance(conn, fact["id"]):
        blocks = conn.execute(
            text("SELECT b.id, b.text, b.bbox, b.ocr_conf, p.page_no FROM ocr_block b "
                 "JOIN document_page p ON p.id = b.page_id WHERE b.id = ANY(:ids) "
                 "ORDER BY b.reading_order"), {"ids": list(p["ocr_block_ids"] or [])}
        ).mappings().all()  # fmt: skip
        page_no = blocks[0]["page_no"] if blocks else None
        if page_no is None and p["page_id"]:
            page_no = conn.execute(text("SELECT page_no FROM document_page WHERE id = :i"),
                                   {"i": p["page_id"]}).scalar_one_or_none()  # fmt: skip
        prov.append({
            "agent": p["agent"], "recorded_at": p["recorded_at"], "bbox_union": p["bbox_union"],
            "extracted_text": p["extracted_text"], "model_stack": p["model_stack"],
            "ocr_blocks": [dict(b) for b in blocks],
            "page_image_url": (f"/api/documents/{p['source_doc_id']}/pages/{page_no}"
                               if page_no and p["source_doc_id"] else None),
        })  # fmt: skip
    return {"fact": fact, "medication": dict(med) if med else None, "provenance": prov,
            "findings": (fact.get("review_note") or {}).get("findings", [])}  # fmt: skip


def open_tasks(conn: Connection) -> list[dict[str, Any]]:
    tasks = conn.execute(
        text("SELECT t.*, d.filename FROM review_task t "
             "LEFT JOIN source_document d ON d.id = t.document_id "
             "WHERE t.status = 'open' ORDER BY t.priority DESC, t.created_at")
    ).mappings().all()  # fmt: skip
    out = []
    for t in tasks:
        facts = conn.execute(
            text("SELECT id, fact_type, local_text, code, code_display, confidence_overall, "
                 "review_state FROM clinical_fact WHERE id = ANY(:ids) AND is_current "
                 "ORDER BY created_at"), {"ids": list(t["fact_ids"])}
        ).mappings().all()  # fmt: skip
        out.append({**dict(t), "facts": [dict(f) for f in facts]})
    return out


def _http(fn):
    try:
        return fn()
    except ReviewError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@router.get("/api/review/tasks")
def api_tasks() -> list[dict[str, Any]]:
    with session_scope() as conn:
        return open_tasks(conn)


@router.get("/api/review/facts/{fact_id}")
def api_fact(fact_id: UUID) -> dict[str, Any]:
    with session_scope() as conn:
        return _http(lambda: fact_detail(conn, fact_id))


@router.post("/api/facts/{fact_id}/review")
def api_review(fact_id: UUID, body: ReviewDecision) -> dict[str, Any]:
    with session_scope() as conn:
        return _http(lambda: decide(conn, fact_id, body))
