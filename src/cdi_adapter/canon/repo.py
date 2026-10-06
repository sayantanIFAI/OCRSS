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
    """Put a validated document into the shared workbasket (idempotent per document)."""
    existing = conn.execute(
        text("SELECT id FROM rv_item WHERE source_document_id = :d AND status IN "
             "('workbasket', 'claimed') LIMIT 1"), {"d": str(document_id)}
    ).scalar_one_or_none()  # fmt: skip
    if existing:
        return existing
    facts = [f for f in repo.get_facts_for_document(conn, document_id) if f["is_current"]]
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
        conn.execute(
            text("INSERT INTO rv_item_element (rv_item_id, fact_id, element_type, payload, state) "
                 "VALUES (:i, :f, :t, CAST(:p AS jsonb), :s)"),
            {"i": item, "f": f["id"], "t": f["fact_type"],
             "p": json.dumps({"local_text": f["local_text"], "code": f["code"]}),
             "s": "held" if f["review_state"] in HELD else "accepted"},
        )  # fmt: skip
    add_action(conn, item, "system", "create", {"document_id": str(document_id), "job_id": job_id})
    return item


def add_action(conn: Connection, item_id, actor: str, action: str,
               detail: dict[str, Any] | None = None) -> None:  # fmt: skip
    conn.execute(
        text("INSERT INTO rv_action (rv_item_id, actor, action, detail) "
             "VALUES (:i, :a, :x, CAST(:d AS jsonb))"),
        {"i": item_id, "a": actor, "x": action, "d": json.dumps(detail or {}, default=str)},
    )  # fmt: skip


def get_item(conn: Connection, item_id: UUID | str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT i.*, d.filename, p.mpi_id, p.name AS patient_name FROM rv_item i "
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


def list_items(conn: Connection, *, status: str, assignee: str | None = None) -> list[dict]:
    sql = ("SELECT i.*, d.filename, p.mpi_id, p.name AS patient_name FROM rv_item i "
           "LEFT JOIN source_document d ON d.id = i.source_document_id "
           "LEFT JOIN patient_identity p ON p.id = i.patient_id WHERE i.status = :s")  # fmt: skip
    params: dict[str, Any] = {"s": status}
    if assignee is not None:
        sql += " AND i.assigned_to = :a"
        params["a"] = assignee
    rows = conn.execute(text(sql + " ORDER BY i.created_at"), params).mappings().all()
    out = []
    for r in rows:
        els = elements(conn, r["id"])
        out.append({**dict(r), "elements": len(els),
                    "held": sum(e["state"] == "held" for e in els)})  # fmt: skip
    return out
