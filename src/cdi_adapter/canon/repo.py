"""Canonical-EMR + review-workbasket data access (ARCHITECTURE §4.5).

``rv_item`` (one per validated document) -> ``rv_item_element`` (one per fact) -> ``rv_action``
(every claim / release / decision / promotion). An element always resolves to the **current**
version of its fact, following the ``supersedes`` chain created by S8 corrections.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter import repo

GOVERNED = ("auto_accepted", "clinician_confirmed", "corrected")
HELD = ("pending", "in_review")

# follow supersedes forward to the current version of a fact
_CURRENT_FACT = """
WITH RECURSIVE chain(id, depth) AS (
    SELECT CAST(:f AS uuid), 0
    UNION ALL
    SELECT c.id, chain.depth + 1 FROM clinical_fact c JOIN chain ON c.supersedes = chain.id
)
SELECT f.* FROM chain JOIN clinical_fact f ON f.id = chain.id ORDER BY chain.depth DESC LIMIT 1
"""


def current_fact(conn: Connection, fact_id: UUID | str) -> dict[str, Any] | None:
    row = conn.execute(text(_CURRENT_FACT), {"f": str(fact_id)}).mappings().first()
    return dict(row) if row else None


def create_rv_item_for_document(
    conn: Connection, document_id: UUID | str, *, job_id: str | None = None
) -> UUID | None:
    """Put a validated document into the shared workbasket (idempotent per document). A document
    already in the workbasket (uploaded again: same bytes = same document, extracted again) keeps
    its item, brought up to date with the document's current facts (2026-09-25: it was left with
    the old, since deleted, facts, i.e. empty)."""
    existing = conn.execute(
        text("SELECT id FROM rv_item WHERE source_document_id = :d AND status IN "
             "('workbasket', 'claimed') LIMIT 1"), {"d": str(document_id)}
    ).scalar_one_or_none()  # fmt: skip
    facts = [f for f in repo.get_facts_for_document(conn, document_id) if f["is_current"]]
    if existing:
        _refresh(conn, existing, facts, job_id)
        return existing
    if not facts:
        return None
    cls = repo.get_latest_classification(conn, document_id)
    item = conn.execute(
        text("INSERT INTO rv_item (patient_id, source_document_id, job_id, doc_type) "
             "VALUES (:p, :d, :j, :t) RETURNING id"),
        {"p": facts[0]["patient_id"], "d": str(document_id), "j": job_id,
         "t": cls["doc_type"] if cls else None},
    ).scalar_one()  # fmt: skip
    for f in facts:
        _add_element(conn, item, f)
    add_action(conn, item, "system", "create", {"document_id": str(document_id), "job_id": job_id})
    return item


def _add_element(conn: Connection, item_id, f: dict[str, Any]) -> None:
    conn.execute(
        text("INSERT INTO rv_item_element (rv_item_id, fact_id, element_type, payload, state) "
             "VALUES (:i, :f, :t, CAST(:p AS jsonb), :s)"),
        {"i": item_id, "f": f["id"], "t": f["fact_type"],
         "p": json.dumps({"local_text": f["local_text"], "code": f["code"]}),
         "s": "held" if f["review_state"] in HELD else "accepted"},
    )  # fmt: skip


def _refresh(conn: Connection, item_id, facts: list[dict[str, Any]], job_id: str | None) -> None:
    """Add an element for every current fact the item does not cover yet. A fact it covers is one
    an element resolves to (a correction is a new version of the element's fact, not a new fact);
    the elements of deleted facts are already gone (ON DELETE CASCADE)."""
    covered = {str(e["fact"]["id"]) for e in elements(conn, item_id) if e["fact"]}
    new = [f for f in facts if str(f["id"]) not in covered]
    if not new:
        return
    for f in new:
        _add_element(conn, item_id, f)
    conn.execute(text("UPDATE rv_item SET job_id = COALESCE(:j, job_id) WHERE id = :i"),
                 {"j": job_id, "i": str(item_id)})  # fmt: skip
    add_action(conn, item_id, "system", "refresh", {"job_id": job_id, "added": len(new)})


def add_action(conn: Connection, item_id, actor: str, action: str,
               detail: dict[str, Any] | None = None) -> None:  # fmt: skip
    conn.execute(
        text("INSERT INTO rv_action (rv_item_id, actor, action, detail) "
             "VALUES (:i, :a, :x, CAST(:d AS jsonb))"),
        {"i": item_id, "a": actor, "x": action, "d": json.dumps(detail or {}, default=str)},
    )  # fmt: skip


def get_item(conn: Connection, item_id: UUID | str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT i.*, d.filename, d.page_count, d.mime_type, p.mpi_id, p.name AS patient_name "
             "FROM rv_item i "
             "LEFT JOIN source_document d ON d.id = i.source_document_id "
             "LEFT JOIN patient_identity p ON p.id = i.patient_id WHERE i.id = :i"),
        {"i": str(item_id)},
    ).mappings().first()  # fmt: skip
    return dict(row) if row else None


def elements(conn: Connection, item_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM rv_item_element WHERE rv_item_id = :i ORDER BY created_at, id"),
        {"i": str(item_id)},
    ).mappings().all()  # fmt: skip
    out = []
    for e in rows:
        f = current_fact(conn, e["fact_id"]) if e["fact_id"] else None
        state = f["review_state"] if f else None
        out.append({**dict(e), "fact": f,
                    "state": ("held" if state in HELD else
                              "rejected" if state == "rejected" else
                              "corrected" if state == "corrected" else "accepted")})  # fmt: skip
    return out


def list_items(conn: Connection, *, status: str, assignee: str | None = None,
               limit: int | None = None) -> list[dict]:  # fmt: skip
    sql = ("SELECT i.*, d.filename, p.mpi_id, p.name AS patient_name FROM rv_item i "
           "LEFT JOIN source_document d ON d.id = i.source_document_id "
           "LEFT JOIN patient_identity p ON p.id = i.patient_id WHERE i.status = :s")  # fmt: skip
    params: dict[str, Any] = {"s": status}
    if assignee is not None:
        sql += " AND i.assigned_to = :a"
        params["a"] = assignee
    # newest activity first (created, or refreshed by a new upload of the document): a new
    # upload is at the top, not under every older item (2026-09-25)
    order = (
        " ORDER BY (SELECT max(a.at) FROM rv_action a WHERE a.rv_item_id = i.id) DESC"
        " NULLS LAST, i.created_at DESC"
    )
    if limit is not None:
        order += " LIMIT :n"
        params["n"] = limit
    rows = conn.execute(text(sql + order), params).mappings().all()
    out = []
    for r in rows:
        els = elements(conn, r["id"])
        out.append({**dict(r), "elements": len(els),
                    "held": sum(e["state"] == "held" for e in els)})  # fmt: skip
    return out


# Customer 360 (backlog E7-S8, ARCHITECTURE §4.5 / §7.1): one patient's canonical record read from
# the cn_* tables, not from the per-document clinical_fact rows. Each section's columns, in order.
C360_SECTIONS: dict[str, tuple[str, str]] = {
    "conditions": ("cn_condition", "code_system, code, display, clinical_status, verification, "
                   "onset"),
    "medications": ("cn_medication", "code_system, code, drug_text, strength_num, strength_unit, "
                    "dose_num, dose_unit_ucum, route, frequency_code, frequency_per_day, "
                    "duration_days, instructions, status"),
    "lab_results": ("cn_lab_result", "test_system, test_code, test_display, value_num, value_text, "
                    "unit_ucum, ref_range_low, ref_range_high, ref_range_text, abnormal_flag, "
                    "effective_time"),
    "lab_orders": ("cn_lab_order", "code_system, code, test_text, status"),
    "vital_signs": ("cn_vital_sign", "code_system, code, display, value_num, value_text, "
                    "unit_ucum, effective_time"),
    "allergies": ("cn_allergy", "code_system, code, substance, reaction, clinical_status"),
}  # fmt: skip


def identifiers(p: dict[str, Any]) -> list[dict[str, str]]:
    """The patient's identifiers, each kept as its own entry: the ABHA number and a legacy MRN
    coexist; neither stands in for the other (ARCHITECTURE §6)."""
    out = [{"type": "MPI", "value": p["mpi_id"]}] if p.get("mpi_id") else []
    for key, label in (("abha_number", "ABHA number"), ("abha_address", "ABHA address"),
                       ("legacy_mrn", "Legacy MRN")):  # fmt: skip
        if p.get(key):
            out.append({"type": label, "value": p[key]})
    return out


def customer_360(conn: Connection, mpi_id: str) -> dict[str, Any] | None:
    """Demographics, identifiers, documents and the canonical clinical facts of one patient,
    across every document. A re-approved item keeps only its latest promotion: each approval
    writes all of an item's rows in one transaction, so they share one ``recorded_at``."""
    p = conn.execute(
        text("SELECT id, mpi_id, name, sex, birth_date, abha_number, abha_address, legacy_mrn, "
             "identity_confidence FROM patient_identity WHERE mpi_id = :m"), {"m": mpi_id},
    ).mappings().first()  # fmt: skip
    if p is None:
        return None
    pid = str(p["id"])
    docs = conn.execute(
        text("SELECT d.id, d.filename, d.status, d.page_count, d.created_at, "
             "  (SELECT c.doc_type FROM doc_classification c WHERE c.document_id = d.id "
             "   ORDER BY c.created_at DESC LIMIT 1) AS doc_type, "
             "  (SELECT i.status FROM rv_item i WHERE i.source_document_id = d.id "
             "   ORDER BY i.created_at DESC LIMIT 1) AS review_status "
             "FROM source_document d WHERE d.patient_id = :p ORDER BY d.created_at DESC"),
        {"p": pid},
    ).mappings().all()  # fmt: skip
    out: dict[str, Any] = {
        "patient": {
            k: p[k] for k in ("mpi_id", "name", "sex", "birth_date", "identity_confidence")
        },  # fmt: skip
        "identifiers": identifiers(dict(p)),
        "documents": [dict(d) for d in docs],
    }
    for key, (table, cols) in C360_SECTIONS.items():
        own = ", ".join("t." + c.strip() for c in cols.split(","))
        rows = conn.execute(
            text(f"SELECT {own}, t.review_state, t.recorded_by, t.recorded_at, "
                 "  d.id AS document_id, d.filename AS document "
                 f"FROM {table} t "
                 "LEFT JOIN rv_item i ON i.id = t.rv_item_id "
                 "LEFT JOIN source_document d ON d.id = i.source_document_id "
                 "WHERE t.patient_id = :p AND (t.rv_item_id IS NULL OR t.recorded_at = "
                 f"  (SELECT max(u.recorded_at) FROM {table} u WHERE u.rv_item_id = t.rv_item_id)) "
                 "ORDER BY t.recorded_at DESC"),
            {"p": pid},
        ).mappings().all()  # fmt: skip
        out[key] = [dict(r) for r in rows]
    out["bundles"] = [dict(r) for r in conn.execute(
        text("SELECT b.artifact_type, b.status, b.created_at, d.filename AS document "
             "FROM cn_fhir_bundle b LEFT JOIN rv_item i ON i.id = b.rv_item_id "
             "LEFT JOIN source_document d ON d.id = i.source_document_id "
             "WHERE b.patient_id = :p ORDER BY b.created_at DESC"), {"p": pid},
    ).mappings()]  # fmt: skip
    return out
