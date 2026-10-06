"""The cross-engine concordance signal (backlog E4-S11): ``ocr_agreement`` (migration 0018).

E2-S11's disagreement engine records here, for one handwritten crop, whether two independent
recognizer readings agree: ``record_agreement`` writes one append-only row per crop pair, field
domain and algorithm version; ``agreements_for`` reads them back (newest first). Nothing in the
pipeline writes rows yet: the second engine and its comparison are E2-S11.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

STATES = ("AGREE", "PARTIAL", "MATERIAL")


def _unit(name: str, v: float | None) -> None:
    if v is not None and not 0 <= v <= 1:
        raise ValueError(f"{name} must be between 0 and 1, got {v}")


def record_agreement(
    conn: Connection,
    *,
    observation_id_a: UUID | str,
    observation_id_b: UUID | str,
    disagreement_state: str,
    agreement_score: float,
    algorithm_version: str,
    field_domain: str | None = None,
    trocr_confidence: float | None = None,
    qwen_confidence: float | None = None,
    reason: str | None = None,
) -> UUID:
    """Write the comparison of two readings of the same crop; never overwrites an earlier one."""
    if disagreement_state not in STATES:
        raise ValueError(f"disagreement_state must be one of {STATES}")
    for name, v in (("agreement_score", agreement_score), ("trocr_confidence", trocr_confidence),
                    ("qwen_confidence", qwen_confidence)):  # fmt: skip
        _unit(name, v)
    if not algorithm_version:
        raise ValueError("algorithm_version is required")
    a, b = str(observation_id_a), str(observation_id_b)
    if a == b:
        raise ValueError("two different observations are compared")
    rows = conn.execute(
        text("SELECT id, document_id, page_id FROM ocr_observation "
             "WHERE id IN (CAST(:a AS uuid), CAST(:b AS uuid))"), {"a": a, "b": b},
    ).all()  # fmt: skip
    if len(rows) != 2:
        raise LookupError("both observations must exist in ocr_observation")
    if len({(r.document_id, r.page_id) for r in rows}) != 1:
        raise ValueError("the two observations must be readings of the same page")
    return conn.execute(
        text("INSERT INTO ocr_agreement (observation_id_a, observation_id_b, field_domain, "
             "  disagreement_state, agreement_score, trocr_confidence, qwen_confidence, reason, "
             "  algorithm_version) "
             "VALUES (:a, :b, :fd, :st, :sc, :tc, :qc, :r, :v) RETURNING id"),
        {"a": a, "b": b, "fd": field_domain, "st": disagreement_state, "sc": agreement_score,
         "tc": trocr_confidence, "qc": qwen_confidence, "r": reason, "v": algorithm_version},
    ).scalar_one()  # fmt: skip


def agreements_for(conn: Connection, observation_id: UUID | str) -> list[dict[str, Any]]:
    """Every recorded comparison involving an observation, newest first."""
    return [dict(r) for r in conn.execute(
        text("SELECT * FROM ocr_agreement WHERE observation_id_a = CAST(:o AS uuid) "
             "OR observation_id_b = CAST(:o AS uuid) ORDER BY computed_at DESC, id"),
        {"o": str(observation_id)},
    ).mappings()]  # fmt: skip
