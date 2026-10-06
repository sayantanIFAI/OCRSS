"""A document's agent decisions, rebuilt from ``agent_trace`` (backlog E12-S5, §4.3).

``agent_trace`` holds every step an agent took on a document - the medication agent's model calls,
catalogue lookups and guard decisions, the resolution cascade's decisions, every tool-registry call
- in the order they were written, with input, output and confidence. It is append-only and each
step is also in ``audit_log`` (migration 0020).

``decisions`` reads the trace alone for the sequence, then cross-references it:

* ``facts`` - the document's current facts, each with the agent steps about it (by ``fact_ref``)
  and its ``fact_provenance`` rows;
* ``superseded`` - steps about facts a re-extraction has since purged (still in the trace, with
  the fact id they decided on);
* ``gaps`` - what would make the record incomplete: a step with no ``audit_log`` row, a tool call
  for the document with no trace step. ``complete`` is true when there are none.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, UUID) else v) for k, v in row.items()}


def steps(conn: Connection, document_id: UUID | str) -> list[dict[str, Any]]:
    """Every agent step on the document, in the order it was written."""
    rows = conn.execute(
        text("SELECT t.id, t.seq, t.agent, t.run_id, t.step, t.tool, t.fact_ref, "
             "  t.fact_id IS NOT NULL AS fact_current, t.ai_event_id, t.confidence, t.input, "
             "  t.output, t.duration_ms, t.tool_call_id, tc.status AS tool_status, "
             "  a.id AS audit_log_id, t.created_at "
             "FROM agent_trace t "
             "LEFT JOIN agent_tool_call tc ON tc.id = t.tool_call_id "
             "LEFT JOIN audit_log a ON a.entity = 'agent_trace' AND a.entity_id = t.id::text "
             "WHERE t.document_id = CAST(:d AS uuid) ORDER BY t.id"),
        {"d": str(document_id)},
    ).mappings()  # fmt: skip
    return [_jsonable(dict(r)) for r in rows]


def _facts(conn: Connection, document_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT id, fact_type, review_state FROM clinical_fact "
             "WHERE CAST(:d AS uuid) = ANY(source_doc_ids) ORDER BY id"),
        {"d": document_id},
    ).mappings()  # fmt: skip
    return [_jsonable(dict(r)) for r in rows]


def _provenance(conn: Connection, fact_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {f: [] for f in fact_ids}
    if not fact_ids:
        return out
    rows = conn.execute(
        text("SELECT id, fact_id, agent, ocr_block_ids, pipeline_run_ids, recorded_at "
             "FROM fact_provenance WHERE fact_id = ANY(CAST(:f AS uuid[])) "
             "ORDER BY recorded_at, id"),
        {"f": fact_ids},
    ).mappings()  # fmt: skip
    for r in rows:
        d = _jsonable(dict(r))
        d["ocr_block_ids"] = [str(x) for x in r["ocr_block_ids"]]
        d["pipeline_run_ids"] = [str(x) for x in r["pipeline_run_ids"]]
        out[d.pop("fact_id")].append(d)
    return out


def decisions(conn: Connection, document_id: UUID | str) -> dict[str, Any]:
    doc = str(document_id)
    seq = steps(conn, doc)
    facts = _facts(conn, doc)
    prov = _provenance(conn, [f["id"] for f in facts])
    current = {f["id"] for f in facts}
    for f in facts:
        f["agent_steps"] = [s["id"] for s in seq if s["fact_ref"] == f["id"]]
        f["provenance"] = prov[f["id"]]
    untraced_calls = conn.execute(
        text("SELECT c.id FROM agent_tool_call c WHERE c.document_id = CAST(:d AS uuid) "
             "AND NOT EXISTS (SELECT 1 FROM agent_trace t WHERE t.tool_call_id = c.id) "
             "ORDER BY c.id"),
        {"d": doc},
    ).scalars().all()  # fmt: skip
    gaps = {"steps_without_audit": [s["id"] for s in seq if s["audit_log_id"] is None],
            "tool_calls_without_step": list(untraced_calls)}  # fmt: skip
    return {
        "document_id": doc,
        "steps": seq,
        "facts": facts,
        "superseded": [s["id"] for s in seq if s["fact_ref"] and s["fact_ref"] not in current],
        "gaps": gaps,
        "complete": not any(gaps.values()),
    }
