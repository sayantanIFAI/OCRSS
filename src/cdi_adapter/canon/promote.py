"""Workbasket -> worklist -> promote (ARCHITECTURE §4.5).

``claim`` moves an item from the shared workbasket into one reviewer's worklist.
``promote_review_item(conn, rv_item_id, reviewer)`` (on approve) copies every **governed** fact of
the item into the canonical ``cn_*`` layer, records the ``ai_event -> ai_suggestion ->
ai_clinical_verification`` chain, builds the canonical FHIR bundle
(``fhir.canonical.build_bundle``), checks it with ``fhir.validate_abdm.validate`` and stores it in
``cn_fhir_bundle``. Only governed facts are copied into ``cn_*``; a fact still held is not.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter import metrics, repo
from cdi_adapter.canon import repo as canon
from cdi_adapter.fhir import canonical, validate_abdm
from cdi_adapter.fhir.resources import artifact_for


class WorkflowError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _item(conn: Connection, item_id, *, lock: bool = False) -> dict[str, Any]:
    if lock:
        # a decision holds the item's row until it commits: a second approve (a double click, or
        # the other web instance) waits, then sees "approved" and is refused. 2026-09-25: approve
        # runs the validator for seconds, and 5 clicks promoted the same item 5 times
        conn.execute(text("SELECT 1 FROM rv_item WHERE id = :i FOR UPDATE"), {"i": str(item_id)})
    item = canon.get_item(conn, item_id)
    if item is None:
        raise WorkflowError(404, "review item not found")
    return item


def claim(conn: Connection, item_id: UUID | str, reviewer: str) -> dict[str, Any]:
    n = conn.execute(
        text("UPDATE rv_item SET status = 'claimed', assigned_to = :r, claimed_at = now() "
             "WHERE id = :i AND status = 'workbasket'"), {"i": str(item_id), "r": reviewer}
    ).rowcount  # fmt: skip
    if n == 0:
        item = _item(conn, item_id)
        raise WorkflowError(
            409,
            f"item is {item['status']}"
            + (f" by {item['assigned_to']}" if item["assigned_to"] else ""),
        )
    canon.add_action(conn, item_id, reviewer, "claim")
    metrics.REVIEW_ITEMS.labels("claim").inc()
    repo.write_audit(conn, actor=reviewer, action="update", entity="rv_item",
                     entity_id=item_id, detail={"action": "claim"})  # fmt: skip
    return _item(conn, item_id)


def _assert_claimed(item: dict[str, Any], reviewer: str, override: bool = False) -> None:
    """§4.5 workflow: an item is claimed into a worklist before it is approved or rejected, and
    only by the reviewer whose worklist holds it (backlog E7-S1). ``override`` (an admin) may act
    on another reviewer's claimed item; the decision is still recorded under the admin's name."""
    if item["status"] != "claimed":
        raise WorkflowError(409, f"item is {item['status']}")
    if item["assigned_to"] != reviewer and not override:
        raise WorkflowError(403, f"item is claimed by {item['assigned_to']}")


def reject_item(conn: Connection, item_id, reviewer: str, reason: str | None, *,
                override: bool = False) -> dict[str, Any]:  # fmt: skip
    item = _item(conn, item_id, lock=True)
    _assert_claimed(item, reviewer, override)
    conn.execute(text("UPDATE rv_item SET status = 'rejected', decided_at = now() WHERE id = :i"),
                 {"i": str(item_id)})  # fmt: skip
    canon.add_action(conn, item_id, reviewer, "reject", {"reason": reason})
    metrics.REVIEW_ITEMS.labels("reject").inc()
    repo.write_audit(conn, actor=reviewer, action="update", entity="rv_item", entity_id=item_id,
                     detail={"action": "reject", "reason": reason})  # fmt: skip
    return _item(conn, item_id)


def _cn_common(f: dict[str, Any], item_id, reviewer: str) -> dict[str, Any]:
    return {"patient_id": f["patient_id"], "rv_item_id": str(item_id), "source_fact_id": f["id"],
            "encounter_id": f["encounter_id"], "review_state": f["review_state"],
            "recorded_by": reviewer}  # fmt: skip


def _insert(conn: Connection, table: str, row: dict[str, Any]) -> None:
    cols = list(row)
    conn.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) "
                      f"VALUES ({', '.join(':' + c for c in cols)})"), row)  # fmt: skip


def _promote_fact(conn: Connection, f: dict[str, Any], item_id, reviewer: str) -> str | None:
    c = _cn_common(f, item_id, reviewer)
    t = f["fact_type"]
    if t == "condition":
        _insert(
            conn,
            "cn_condition",
            {
                **c,
                "code_system": f["code_system"],
                "code": f["code"],
                "display": f["code_display"] or f["local_text"],
                "clinical_status": f["clinical_status"] or "active",
                "verification": "confirmed",
                "onset": f["onset"],
            },
        )
        return "cn_condition"
    if t == "medication":
        med_row = conn.execute(text("SELECT * FROM medication_detail WHERE fact_id = :i"),
                               {"i": f["id"]}).mappings().first()  # fmt: skip
        m: dict[str, Any] = dict(med_row) if med_row else {}
        _insert(
            conn,
            "cn_medication",
            {
                **c,
                "code_system": f["code_system"],
                "code": f["code"],
                "drug_text": m.get("drug_text") or f["local_text"],
                "strength_num": m.get("strength_num"),
                "strength_unit": m.get("strength_unit"),
                "dose_num": m.get("dose_num"),
                "dose_unit_ucum": m.get("dose_unit_ucum"),
                "route": m.get("route"),
                "frequency_code": m.get("frequency_code"),
                "frequency_per_day": m.get("frequency_per_day"),
                "duration_days": m.get("duration_days"),
                "instructions": m.get("instructions"),
            },
        )
        return "cn_medication"
    if t == "lab_result":
        # §4.5: the test-code column is test_code, not code
        _insert(conn, "cn_lab_result", {
            **c, "test_system": f["code_system"], "test_code": f["code"],
            "test_display": f["code_display"] or f["local_text"], "value_num": f["value_num"],
            "value_text": f["value_text"], "unit_ucum": f["value_unit_ucum"],
            "ref_range_low": f["ref_range_low"], "ref_range_high": f["ref_range_high"],
            "ref_range_text": f["ref_range_text"], "abnormal_flag": f["abnormal_flag"],
            "effective_time": f["effective_time"]})  # fmt: skip
        return "cn_lab_result"
    if t == "vital_sign":
        _insert(conn, "cn_vital_sign", {**c, "code_system": f["code_system"], "code": f["code"],
                                        "display": f["code_display"] or f["local_text"],
                                        "value_num": f["value_num"],
                                        "value_text": f["value_text"],
                                        "unit_ucum": f["value_unit_ucum"],
                                        "effective_time": f["effective_time"]})  # fmt: skip
        return "cn_vital_sign"
    if t == "allergy":
        _insert(conn, "cn_allergy", {**c, "code_system": f["code_system"], "code": f["code"],
                                     "substance": f["code_display"] or f["local_text"],
                                     "reaction": f["value_text"],
                                     "clinical_status": "active"})  # fmt: skip
        return "cn_allergy"
    if t == "lab_order":  # a test the prescription advises (owner request 2026-09-28)
        _insert(conn, "cn_lab_order", {**c, "code_system": f["code_system"], "code": f["code"],
                                       "test_text": f["local_text"]})  # fmt: skip
        return "cn_lab_order"
    return None  # other fact types stay in the per-document record only


def promote_review_item(conn: Connection, rv_item_id: UUID | str, reviewer: str, *,
                        override: bool = False) -> dict[str, Any]:  # fmt: skip
    item = _item(conn, rv_item_id, lock=True)
    _assert_claimed(item, reviewer, override)
    els = canon.elements(conn, rv_item_id)

    model = conn.execute(text("SELECT model_name FROM extraction WHERE document_id = :d "
                              "ORDER BY created_at DESC LIMIT 1"),
                         {"d": item["source_document_id"]}).scalar_one_or_none()  # fmt: skip
    event = conn.execute(
        text("INSERT INTO ai_event (rv_item_id, source_document_id, kind, model_stack) "
             "VALUES (:i, :d, 'extraction', CAST(:m AS jsonb)) RETURNING id"),
        {"i": str(rv_item_id), "d": item["source_document_id"],
         "m": json.dumps({"extractor": model})},
    ).scalar_one()  # fmt: skip

    promoted: dict[str, int] = {}
    original = {str(o["id"]): o
                for o in repo.get_facts_for_document(conn, item["source_document_id"])}  # fmt: skip
    for e in els:
        f = e["fact"]
        if f is None:
            continue
        machine = original.get(str(e["fact_id"]), f)  # what the model proposed
        sug = conn.execute(
            text(
                "INSERT INTO ai_suggestion (ai_event_id, fact_id, element_type, suggested, "
                "  confidence) VALUES (:e, :f, :t, CAST(:s AS jsonb), :c) RETURNING id"
            ),
            {
                "e": event,
                "f": e["fact_id"],
                "t": e["element_type"],
                "s": json.dumps(
                    {
                        "local_text": machine["local_text"],
                        "value_num": machine["value_num"],
                        "code": machine["code"],
                    },
                    default=str,
                ),
                "c": machine["confidence_overall"],
            },
        ).scalar_one()
        if f["review_state"] in canon.HELD:
            continue  # nobody has decided this fact: no verification is recorded, nothing promoted
        decision = {"rejected": "rejected", "corrected": "corrected"}.get(f["review_state"],
                                                                         "accepted")  # fmt: skip
        conn.execute(
            text("INSERT INTO ai_clinical_verification (ai_suggestion_id, reviewer, decision, "
                 "  final_value) VALUES (:s, :r, :d, CAST(:v AS jsonb))"),
            {"s": sug, "r": reviewer, "d": decision,
             "v": json.dumps({"local_text": f["local_text"], "value_num": f["value_num"],
                              "code": f["code"]}, default=str)},
        )  # fmt: skip
        if f["review_state"] in canon.GOVERNED and f["is_current"]:
            table = _promote_fact(conn, f, rv_item_id, reviewer)
            if table:
                promoted[table] = promoted.get(table, 0) + 1

    bundle = canonical.build_bundle(conn, rv_item_id)
    artifact = artifact_for(item["doc_type"])[0]
    result = validate_abdm.validate(bundle, artifact)
    status = "ready_to_share" if result["valid"] else "draft"
    conn.execute(
        text("INSERT INTO cn_fhir_bundle (patient_id, rv_item_id, artifact_type, bundle, "
             "  bundle_hash, validation, status) VALUES (:p, :i, :a, CAST(:b AS jsonb), :h, "
             "  CAST(:v AS jsonb), :s)"),
        {"p": item["patient_id"], "i": str(rv_item_id), "a": artifact, "b": json.dumps(bundle),
         "h": hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest(),
         "v": json.dumps(result), "s": status},
    )  # fmt: skip
    conn.execute(text("UPDATE rv_item SET status = 'approved', decided_at = now() WHERE id = :i"),
                 {"i": str(rv_item_id)})  # fmt: skip
    canon.add_action(conn, rv_item_id, reviewer, "approve")
    metrics.REVIEW_ITEMS.labels("approve").inc()
    canon.add_action(conn, rv_item_id, reviewer, "promote", {"promoted": promoted})
    repo.write_audit(conn, actor=reviewer, action="create", entity="cn_fhir_bundle",
                     entity_id=rv_item_id,
                     detail={"promoted": promoted, "status": status,
                             "engine": result["engine"]})  # fmt: skip
    return {"rv_item_id": str(rv_item_id), "promoted": promoted, "artifact_type": artifact,
            "status": status, "validation": result}  # fmt: skip
