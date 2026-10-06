"""Reviewer corrections -> the calibration training table (backlog E4-S1, ARCHITECTURE §4.2-§4.3).

S8 already captures every decision: ``review.decide`` sets ``clinician_confirmed`` or ``rejected``
on the fact, or writes a corrected version that ``supersedes`` it, and signs a ``fact_provenance``
row (``model_stack = {"reviewer": "human"}``, ``agent`` = the reviewer). ``export_dataset`` turns
that into a versioned training table for the E4-S2 calibrator. Migration0025 also snapshots
each signed decision atomically with S8, retaining it when its clinical projection is purged:

* one example per **adjudicated fact**: the correction chain from the machine's proposal (the fact
  that ``supersedes`` nothing) to its final version, when that version carries a human decision;
* the **triple**: proposed value, corrected value (none when rejected), ``doc_type``,
  ``fact_type``, and the machine's confidence (the S6 gate's calibrated ``confidence_overall`` on
  the proposal) - plus the decision, a 0/1 label (was the machine's value right), the component
  confidences, the machine state before the decision (auto-accepted or held), the reviewer, the
  decision time and the model stack. No patient identifier is copied;
* a **version** is a full snapshot with the SHA-256 of its content; an export whose content equals
  the latest version writes nothing. Published versions are append-only in the database
  (migration 0015). The nightly cron (``bootstrap_pod.sh``) runs
  ``python -m cdi_adapter.training export``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.db import session_scope

SCHEMA_VERSION = "calibration_training.v1"
HUMAN = ("clinician_confirmed", "corrected", "rejected")
_VALUE = ("local_text", "value_num", "value_unit_ucum", "value_text", "code_system", "code",
          "code_display")  # fmt: skip
_MED = ("drug_text", "strength_num", "strength_unit", "dose_num", "dose_unit_ucum",
        "frequency_code", "duration_days")  # fmt: skip

# every correction chain: its machine proposal (supersedes nothing) and its final version
_CHAINS = """
WITH RECURSIVE chain(root_id, fact_id, depth) AS (
    SELECT id, id, 0 FROM clinical_fact WHERE supersedes IS NULL
    UNION ALL
    SELECT chain.root_id, c.id, chain.depth + 1
    FROM clinical_fact c JOIN chain ON c.supersedes = chain.fact_id
),
final AS (
    SELECT DISTINCT ON (root_id) root_id, fact_id AS final_id, depth
    FROM chain ORDER BY root_id, depth DESC
)
SELECT final.root_id, final.final_id, final.depth
FROM final JOIN clinical_fact f ON f.id = final.final_id
WHERE f.review_state IN ('clinician_confirmed', 'corrected', 'rejected')
  AND EXISTS (SELECT 1 FROM fact_provenance p WHERE p.fact_id = f.id
              AND p.model_stack ? 'reviewer' AND p.agent IS NOT NULL)
ORDER BY final.root_id
"""


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _value(fact: dict[str, Any], med: dict[str, Any] | None) -> dict[str, Any]:
    out = {k: _plain(fact.get(k)) for k in _VALUE}
    if fact["fact_type"] == "medication":
        out |= {k: _plain((med or {}).get(k)) for k in _MED}
    return out


def _fact(conn: Connection, fact_id: Any) -> dict[str, Any]:
    return dict(conn.execute(text("SELECT * FROM clinical_fact WHERE id = :f"),
                             {"f": str(fact_id)}).mappings().one())  # fmt: skip


def _med(conn: Connection, fact_id: Any) -> dict[str, Any] | None:
    row = conn.execute(text("SELECT * FROM medication_detail WHERE fact_id = :f"),
                       {"f": str(fact_id)}).mappings().first()  # fmt: skip
    return dict(row) if row else None


def _example(conn: Connection, root_id: Any, final_id: Any, depth: int) -> dict[str, Any]:
    root, final = _fact(conn, root_id), _fact(conn, final_id)
    proposed = _value(root, _med(conn, root_id))
    if final["review_state"] == "rejected":
        decision, corrected = "rejected", None
    elif depth == 0 and final["review_state"] == "clinician_confirmed":
        decision, corrected = "confirmed", proposed
    else:
        decision, corrected = "corrected", _value(final, _med(conn, final_id))
    label = int(decision == "confirmed" or (decision == "corrected" and corrected == proposed))
    doc = (root.get("source_doc_ids") or [None])[0]
    doc_type = conn.execute(
        text("SELECT doc_type FROM doc_classification WHERE document_id = :d "
             "ORDER BY created_at DESC LIMIT 1"), {"d": str(doc)},
    ).scalar_one_or_none() if doc else None  # fmt: skip
    signed = conn.execute(
        text("SELECT agent, recorded_at FROM fact_provenance WHERE fact_id = :f "
             "AND model_stack ? 'reviewer' ORDER BY recorded_at DESC LIMIT 1"),
        {"f": str(final_id)},
    ).first()  # fmt: skip
    stack = conn.execute(
        text("SELECT model_stack FROM fact_provenance WHERE fact_id = :f "
             "AND NOT (model_stack ? 'reviewer') ORDER BY recorded_at LIMIT 1"),
        {"f": str(root_id)},
    ).scalar_one_or_none()  # fmt: skip
    auto = conn.execute(
        text("SELECT 1 FROM verified_fact WHERE fact_id = :f "
             "AND verification_method = 'auto_gate' LIMIT 1"), {"f": str(root_id)},
    ).first()  # fmt: skip
    return {
        "proposed_fact_id": str(root_id), "final_fact_id": str(final_id),
        "document_id": str(doc) if doc else None, "doc_type": doc_type,
        "fact_type": root["fact_type"], "decision": decision,
        "proposed": proposed, "corrected": corrected,
        "confidence": _plain(root.get("confidence_overall")),
        "confidence_extract": _plain(root.get("confidence_extract")),
        "confidence_ocr": _plain(root.get("confidence_ocr")),
        "confidence_terminology": _plain(root.get("confidence_terminology")),
        "machine_state": "auto_accepted" if auto else "held", "label": label,
        "reviewer": signed.agent if signed else None,
        "decided_at": signed.recorded_at.isoformat() if signed else None,
        "model_stack": stack,
    }  # fmt: skip


def record_decision(conn: Connection, final_id: Any) -> None:
    """Capture a signed decision before its mutable clinical projection can be purged."""
    row = conn.execute(
        text("""
        WITH RECURSIVE ancestry AS (
            SELECT id, supersedes, 0 AS depth, ARRAY[id] AS visited
            FROM clinical_fact WHERE id = :f
            UNION ALL
            SELECT parent.id, parent.supersedes, child.depth + 1, child.visited || parent.id
            FROM clinical_fact parent JOIN ancestry child ON parent.id = child.supersedes
            WHERE NOT parent.id = ANY(child.visited)
        ) SELECT id, depth FROM ancestry WHERE supersedes IS NULL
    """),
        {"f": str(final_id)},
    ).one()
    example = _example(conn, row.id, final_id, row.depth)
    if not example["reviewer"] or not example["decided_at"]:
        raise ValueError("adjudication requires signed reviewer provenance")
    conn.execute(
        text("""
        INSERT INTO calibration_adjudication (proposed_fact_id, final_fact_id, example)
        VALUES (:root, :final, CAST(:example AS jsonb))
    """),
        {"root": str(row.id), "final": str(final_id), "example": json.dumps(example)},
    )


def preserve_document_decisions(conn: Connection, document_id: Any) -> None:
    """Also retain signed decisions made before this snapshot mechanism was deployed."""
    finals = (
        conn.execute(
            text("""
        SELECT f.id FROM clinical_fact f
        WHERE :d = ANY(f.source_doc_ids)
          AND f.review_state IN ('clinician_confirmed', 'corrected', 'rejected')
          AND NOT EXISTS (SELECT 1 FROM clinical_fact child WHERE child.supersedes = f.id)
          AND EXISTS (SELECT 1 FROM fact_provenance p WHERE p.fact_id = f.id
                      AND p.model_stack ? 'reviewer' AND p.agent IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM calibration_adjudication a WHERE a.final_fact_id = f.id)
    """),
            {"d": str(document_id)},
        )
        .scalars()
        .all()
    )
    for fact_id in finals:
        record_decision(conn, fact_id)


def collect(conn: Connection) -> list[dict[str, Any]]:
    """Latest signed decision per proposal, including projections removed by re-extraction."""
    rows = {
        str(r.root_id): _example(conn, r.root_id, r.final_id, r.depth)
        for r in conn.execute(text(_CHAINS))
    }
    historical = conn.execute(
        text("""
        SELECT DISTINCT ON (proposed_fact_id) proposed_fact_id, example
        FROM calibration_adjudication ORDER BY proposed_fact_id, id DESC
    """)
    ).all()
    rows.update({str(r.proposed_fact_id): r.example for r in historical})
    return [rows[key] for key in sorted(rows)]


def content_sha256(rows: list[dict[str, Any]]) -> str:
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def export(conn: Connection, *, actor: str = "calibration-export",
           force: bool = False) -> dict[str, Any]:  # fmt: skip
    """Write the next version when the content changed (or ``force``); return a summary."""
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('calibration_export'))"))
    rows = collect(conn)
    digest = content_sha256(rows)
    latest = conn.execute(text("SELECT version, content_sha256 FROM calibration_dataset "
                               "ORDER BY version DESC LIMIT 1")).first()  # fmt: skip
    if latest and latest.content_sha256 == digest and not force:
        return {"version": latest.version, "rows": len(rows), "sha256": digest, "unchanged": True}
    version = (latest.version if latest else 0) + 1
    by_decision = {d: sum(r["decision"] == d for r in rows)
                   for d in ("confirmed", "corrected", "rejected")}  # fmt: skip
    dataset_id = conn.execute(
        text("INSERT INTO calibration_dataset (version, schema_version, content_sha256, "
             "  row_count, source, created_by) "
             "VALUES (:v, :s, :h, :n, CAST(:src AS jsonb), :by) RETURNING id"),
        {"v": version, "s": SCHEMA_VERSION, "h": digest, "n": len(rows),
         "src": json.dumps({"from": "clinical_fact + fact_provenance + calibration_adjudication",
                            **by_decision}),
         "by": actor},
    ).scalar_one()  # fmt: skip
    if rows:
        conn.execute(
            text("INSERT INTO calibration_training_example (dataset_id, proposed_fact_id, "
                 "  final_fact_id, document_id, doc_type, fact_type, decision, proposed, "
                 "  corrected, confidence, confidence_extract, confidence_ocr, "
                 "  confidence_terminology, machine_state, label, reviewer, decided_at, "
                 "  model_stack) "
                 "VALUES (:ds, :proposed_fact_id, :final_fact_id, :document_id, :doc_type, "
                 "  :fact_type, :decision, CAST(:proposed AS jsonb), CAST(:corrected AS jsonb), "
                 "  :confidence, :confidence_extract, :confidence_ocr, :confidence_terminology, "
                 "  :machine_state, :label, :reviewer, :decided_at, "
                 "  CAST(:model_stack AS jsonb))"),
            [{**r, "ds": str(dataset_id), "proposed": json.dumps(r["proposed"]),
              "corrected": json.dumps(r["corrected"]) if r["corrected"] is not None else None,
              "model_stack": json.dumps(r["model_stack"]) if r["model_stack"] else None}
             for r in rows],
        )  # fmt: skip
    repo.write_audit(conn, actor=actor, action="create", entity="calibration_dataset",
                     entity_id=str(dataset_id),
                     detail={"version": version, "rows": len(rows), "sha256": digest})  # fmt: skip
    return {"version": version, "rows": len(rows), "sha256": digest, "unchanged": False,
            **by_decision}  # fmt: skip


def export_dataset(*, actor: str = "calibration-export", force: bool = False) -> dict[str, Any]:
    with session_scope() as conn:
        return export(conn, actor=actor, force=force)


def load(conn: Connection, version: int | None = None) -> list[dict[str, Any]]:
    """A published version's examples (the latest when ``version`` is None), for E4-S2."""
    row = conn.execute(
        text("SELECT id FROM calibration_dataset WHERE version = :v") if version is not None
        else text("SELECT id FROM calibration_dataset ORDER BY version DESC LIMIT 1"),
        {"v": version},
    ).first()  # fmt: skip
    if row is None:
        return []
    return [dict(r) for r in conn.execute(
        text("SELECT * FROM calibration_training_example WHERE dataset_id = :d "
             "ORDER BY proposed_fact_id"), {"d": str(row.id)},
    ).mappings()]  # fmt: skip
