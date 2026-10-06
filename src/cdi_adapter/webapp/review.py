"""S8 human adjudication (ARCHITECTURE §5 S8, §7.1).

* ``GET /api/review/tasks`` — open ``review_task``s with their held facts.
* ``GET /api/review/facts/{fact_id}`` — the fact, its ``medication_detail``, every
  ``fact_provenance`` row -> resolved ``ocr_block`` texts + the ``bbox_union`` + the page image url
  (``/api/documents/{id}/pages/{n}``) + the rule findings.
* ``POST /api/facts/{fact_id}/review`` ``{action: accept|correct|reject, corrections?,
  note?}``: accept -> ``clinician_confirmed``; correct -> ``apply_fact_correction`` (whitelisted
  fields) -> a new current fact ``corrected`` that ``supersedes`` the original; reject ->
  ``rejected``, ``is_current=false``, ``clinical_status='entered-in-error'``.

Every decision writes a ``fact_provenance`` row with ``agent = <reviewer>`` and
``model_stack = {"reviewer": "human"}``, an ``audit_log`` row, and closes the ``review_task`` once
none of its facts remain ``in_review``. ``GET /api/jobs/{id}/fhir`` then re-projects live.
The reviewer is the signed-in user (backlog E7-S1): a ``reviewer`` sent by the client is
ignored, so a decision can never be recorded under someone else's name.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from cdi_adapter import evidence, longitudinal, metrics, repo, summary
from cdi_adapter.db import session_scope
from cdi_adapter.terminology.parse import freq_per_day, is_prn, to_ucum
from cdi_adapter.terminology.service import bind_text
from cdi_adapter.webapp import auth

router = APIRouter()

CORRECTABLE = {
    "local_text", "code", "code_system", "code_display", "value_num", "value_unit_ucum",
    "value_text", "clinical_status", "ref_range_low", "ref_range_high", "ref_range_text",
    "abnormal_flag",
}  # fmt: skip
REVIEWABLE = ("pending", "in_review", "auto_accepted")
# a decided fact stays correctable (the reviewer's Edit) until its review item is approved: from
# then on its values are in the canonical EMR, so a change here would no longer reach it
EDITABLE = ("clinician_confirmed", "corrected")
# a medication's detail (medication_detail) is what the canonical EMR and FHIR carry; a medication
# edit corrects it, not only the fact's text (2026-09-25)
MED_CORRECTABLE = {
    "drug_text", "strength_num", "strength_unit", "dose_num", "dose_unit_ucum", "route",
    "frequency_code", "duration_days", "prn", "instructions", "form",
}  # fmt: skip
_MED_NUMBERS = ("strength_num", "dose_num", "duration_days")
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
    # no reviewer field (E7-S1): the reviewer is the session user; a client-sent one is ignored
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


def _approved(conn: Connection, fact: dict[str, Any]) -> bool:
    """Whether THIS fact is in an approved review item: the item holds the fact, or an earlier
    version of it (a correction supersedes it). Not merely an approved item for the same document:
    a document uploaded again is extracted again into new facts and a new item, which stays
    correctable until it is approved itself (2026-09-28: the older approval blocked every Edit and
    Replace on the re-upload)."""
    return conn.execute(
        text("WITH RECURSIVE chain(id) AS (SELECT CAST(:f AS uuid) UNION ALL "
             "  SELECT c.supersedes FROM clinical_fact c JOIN chain ON c.id = chain.id "
             "  WHERE c.supersedes IS NOT NULL) "
             "SELECT 1 FROM rv_item_element e JOIN rv_item i ON i.id = e.rv_item_id "
             "WHERE i.status = 'approved' AND e.fact_id IN (SELECT id FROM chain) LIMIT 1"),
        {"f": str(fact["id"])},
    ).first() is not None  # fmt: skip


def _number(field: str, v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ReviewError(422, f"{field} must be a number") from None
    if n < 0:
        raise ReviewError(422, f"{field} must not be negative")
    return n


def _signed(field: str, v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ReviewError(422, f"{field} must be a number") from None


def _med_corrections(fact: dict[str, Any], med: dict[str, Any] | None,
                     c: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    """The medication_detail after the corrections: numbers checked, the frequency's per-day count
    and as-needed flag re-read from the corrected frequency, units as UCUM where known."""
    out = {k: (med or {}).get(k) for k in repo.MED_COLUMNS}
    for k, v in c.items():
        out[k] = _number(k, v) if k in _MED_NUMBERS else (None if v == "" else v)
    if "frequency_code" in c:
        out["frequency_per_day"] = freq_per_day(out["frequency_code"])
        if "prn" not in c:
            out["prn"] = is_prn(out["frequency_code"])
    if "dose_unit_ucum" in c and out["dose_unit_ucum"]:
        out["dose_unit_ucum"] = to_ucum(out["dose_unit_ucum"]) or out["dose_unit_ucum"]
    if not out.get("drug_text"):
        out["drug_text"] = fact["local_text"]
    out["prn"] = bool(out.get("prn"))  # NOT NULL columns keep their defaults
    if out.get("intent") is None:
        out.pop("intent")
    return out


def apply_fact_correction(conn: Connection, fact: dict[str, Any], corrections: dict[str, Any],
                          reviewer: str) -> UUID:  # fmt: skip
    allowed = CORRECTABLE | (MED_CORRECTABLE if fact["fact_type"] == "medication" else set())
    bad = set(corrections) - allowed
    if bad:
        raise ReviewError(422, f"fields not correctable: {sorted(bad)}")
    if not corrections:
        raise ReviewError(422, "correct needs at least one field in corrections")
    fact_c = {k: v for k, v in corrections.items() if k in CORRECTABLE}
    med_c = {k: v for k, v in corrections.items() if k in MED_CORRECTABLE}
    if "value_num" in fact_c:
        fact_c["value_num"] = _number("value_num", fact_c["value_num"])
    for k in ("ref_range_low", "ref_range_high"):  # a range may be below zero (base excess)
        if k in fact_c:
            fact_c[k] = _signed(k, fact_c[k])
    new = {c: fact[c] for c in _COPY_COLS}
    new.update(fact_c)
    if med_c.get("drug_text") and "local_text" not in fact_c:
        new["local_text"] = med_c["drug_text"]  # a medication fact's text is the medicine's name
    if not str(new.get("local_text") or "").strip():
        raise ReviewError(422, "the fact's text must not be empty")
    if "code" in fact_c:
        new["code_status"] = "bound" if fact_c.get("code") else "local_only"
    elif new["local_text"] != fact["local_text"]:
        # the text changed: its code is looked up again, never kept from the old text
        b = bind_text(fact["fact_type"], new["local_text"])
        new.update(code_system=b.concept.system if b else None, code=b.concept.code if b else None,
                   code_display=b.concept.display if b else None,
                   code_status="bound" if b else "local_only")  # fmt: skip
    cols = [*new, "review_state", "supersedes", "is_current"]
    vals = {**new, "review_state": "corrected", "supersedes": fact["id"], "is_current": True}
    new_id = conn.execute(
        text(f"INSERT INTO clinical_fact ({', '.join(cols)}) "
             f"VALUES ({', '.join(':' + c for c in cols)}) RETURNING id"), vals
    ).scalar_one()  # fmt: skip
    row = conn.execute(text("SELECT * FROM medication_detail WHERE fact_id = :i"),
                       {"i": fact["id"]}).mappings().first()  # fmt: skip
    med = dict(row) if row else None
    if med or med_c:
        detail = _med_corrections(fact, med, med_c) if med_c else {
            k: med[k] for k in repo.MED_COLUMNS} if med else {}  # fmt: skip
        repo.insert_medication_detail(conn, new_id, **detail)
        conn.execute(text("UPDATE medication_detail SET rxlike_system = :s, rxlike_code = :c "
                          "WHERE fact_id = :i"),
                     {"s": new["code_system"], "c": new["code"], "i": new_id})  # fmt: skip
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


def decide(conn: Connection, fact_id: UUID | str, d: ReviewDecision,
           reviewer: str) -> dict[str, Any]:  # fmt: skip
    fact = _fact(conn, fact_id)
    editable = d.action == "correct" and fact["review_state"] in EDITABLE
    if not fact["is_current"] or (fact["review_state"] not in REVIEWABLE and not editable):
        raise ReviewError(409, f"fact is {fact['review_state']}; already adjudicated")
    if d.action == "correct" and _approved(conn, fact):
        raise ReviewError(409, "its review item is approved: the fact is in the canonical EMR "
                               "and can no longer be corrected here")  # fmt: skip
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
        result_id = apply_fact_correction(conn, fact, d.corrections or {}, reviewer)
        state = "corrected"
        if fact["fact_type"] == "medication":  # the corrected name is identified in the catalogue
            from cdi_adapter.catalogue.service import rematch_fact

            rematch_fact(conn, result_id)
    _reviewer_provenance(conn, result_id, fact, reviewer)
    # backlog E4-S7: the decision as a reviewer candidate + verified_fact (append-only)
    evidence.record_reviewer_decision(conn, result_id, d.action, reviewer, fact["id"])
    from cdi_adapter.training.calibration import record_decision

    record_decision(conn, result_id)
    repo.write_audit(conn, actor=reviewer, action="update", entity="clinical_fact",
                     entity_id=fact["id"],
                     detail={"action": d.action, "result_fact": str(result_id),
                             "corrections": d.corrections, "note": d.note})  # fmt: skip
    closed = _close_tasks(conn, [str(fact["id"]), str(result_id)])
    # E5-S1: a decided order may now (not) supersede, and a decided condition may now (not)
    # continue an earlier mention
    if fact["fact_type"] == "medication":
        longitudinal.reconcile_patient_medications(conn, fact["patient_id"], actor=reviewer)
    elif fact["fact_type"] == "condition":
        longitudinal.reconcile_patient_problems(conn, fact["patient_id"], actor=reviewer)
    # E5-S2: the decision changed a governed fact, so the summary is rebuilt with it
    summary.refresh_patient_summary(conn, fact["patient_id"])
    metrics.REVIEW_DECISIONS.labels(d.action).inc()
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
            "findings": (fact.get("review_note") or {}).get("findings", []),
            "resolution": _resolution(conn, fact["id"])}  # fmt: skip


def _resolution(conn: Connection, fact_id: Any) -> dict[str, Any]:
    """Backlog E4-S8: how the fact was resolved - the cascade's latest decision (level, outcome,
    escalation cause, per-signal evidence matrix) and every verification recorded for it."""
    from cdi_adapter.resolution.facts import latest_decision

    verified = conn.execute(
        text("SELECT v.verification_method, v.review_state, v.reviewer_id, v.policy_id, "
             "  v.created_at, c.resolved_by_level FROM verified_fact v "
             "JOIN interpretation_candidate c ON c.id = v.candidate_id "
             "WHERE v.fact_id = :f ORDER BY v.created_at"), {"f": str(fact_id)},
    ).mappings().all()  # fmt: skip
    return {"decision": latest_decision(conn, fact_id), "verified": [dict(v) for v in verified]}


def open_tasks(conn: Connection) -> list[dict[str, Any]]:
    tasks = conn.execute(
        text("SELECT t.*, d.filename FROM review_task t "
             "LEFT JOIN source_document d ON d.id = t.document_id "
             "WHERE t.status = 'open' ORDER BY t.priority DESC, t.created_at DESC")  # newest first
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
def api_review(fact_id: UUID, body: ReviewDecision, request: Request) -> dict[str, Any]:
    reviewer = auth.actor(request).username
    with session_scope() as conn:
        return _http(lambda: decide(conn, fact_id, body, reviewer))
