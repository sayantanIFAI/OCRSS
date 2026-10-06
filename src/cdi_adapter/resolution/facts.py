"""The resolution cascade applied to a gated ``clinical_fact`` (backlog E4-S8).

Builds the fact's evidence matrix from what the pipeline records today and runs ``resolve``:

* **tier 1** - one reading per recognizer engine that read the fact's evidence lines
  (``ocr_observation``, E4-S7), or the extractor's quoted span when no line is linked;
* **tier 2** - the S5 terminology binding (how it matched: exact / alias / token / fuzzy);
* **tier 0** (pixel grounding, E4-S4), a second independent reader (E2-S10/S11), exemplars
  (E15-S7) and **tier 3** priors (E15) do not exist yet: recorded as not built, never faked.

Cascade levels found today: L0 (a field that needs no concept: the reading itself), L1 (the text
as written is a concept's display or verified alias), L2 (the same after normalization), L3
(fuzzy). The cohort is ``handwritten`` when a VLM transcription read the evidence, else
``printed``. Each decision is logged as an E12-S5 agent step in ``agent_trace``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter.config import get_settings
from cdi_adapter.resolution.cascade import Decision, LevelResult, Signal, load_policy, resolve
from cdi_adapter.terminology.service import bind_text, normalize

AGENT = "resolution-cascade"
# fact types that need a standard concept to be complete (the S6 terminology rule's list)
CONCEPT_FIELDS = ("condition", "medication", "allergy", "procedure")
_NOT_BUILT_TIERS = (
    {"tier": 0, "reason": "pixel grounding not built (E4-S4)"},
    {"tier": 1, "reason": "no second independent reader (E2-S10/S11), no exemplars (E15-S7)"},
    {"tier": 3, "reason": "no priors yet (E15-S6, E15-S12)"},
)


def _observations(conn: Connection, provenance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = sorted({str(b) for p in provenance for b in (p.get("ocr_block_ids") or [])})
    if not ids:
        return []
    return [dict(r) for r in conn.execute(
        text("SELECT engine, region_kind, ocr_conf FROM ocr_observation "
             "WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids},
    ).mappings()]  # fmt: skip


def candidate_of(fact: dict[str, Any]) -> str:
    """The value being resolved: the bound concept, else the reading itself."""
    if fact.get("code_status") == "bound" and fact.get("code_display"):
        return str(fact["code_display"])
    for key in ("local_text", "value_text", "value_num"):
        if fact.get(key) not in (None, ""):
            return str(fact[key]).strip()
    return ""


def evidence_for(conn: Connection, fact: dict[str, Any], provenance: list[dict[str, Any]]
                 ) -> tuple[list[Signal], dict[str, LevelResult], str]:  # fmt: skip
    cand = candidate_of(fact)
    obs = _observations(conn, provenance)
    handwritten = any(o["engine"] == "vlm" or o["region_kind"] == "handwritten" for o in obs)
    cohort = "handwritten" if handwritten else "printed"
    signals: list[Signal] = []
    for engine in sorted({o["engine"] for o in obs}):
        confs = [float(o["ocr_conf"]) for o in obs if o["engine"] == engine and o["ocr_conf"]]
        n = sum(o["engine"] == engine for o in obs)
        mean = round(sum(confs) / len(confs), 4) if confs else None
        signals.append(Signal(engine, 1, cand, "supports", mean, f"{n} observation(s)"))
    if not obs and any((p.get("extracted_text") or "").strip() for p in provenance):
        signals.append(Signal("extraction-span", 1, cand, "supports",
                              fact.get("confidence_extract"),
                              "the extractor's quoted span (no OCR line linked)"))  # fmt: skip
    if len({o["engine"] for o in obs}) > 1:
        # Different engines attached to a fact do not prove that their field readings agree.
        # E4-S11 deliberately does not join the concordance table into candidate resolution.
        signals.append(
            Signal(
                "uncompared-readings",
                1,
                cand,
                "contradicts",
                reason="multiple engines require a validated field comparison",
            )
        )
    levels: dict[str, LevelResult] = {}
    local = str(fact.get("local_text") or "")
    b = bind_text(fact["fact_type"], local) if fact.get("code_status") == "bound" else None
    if b is not None:
        signals.append(Signal(f"terminology:{b.how}", 2, cand, "supports",
                              fact.get("confidence_terminology"),
                              f"{b.concept.system} {b.concept.code}"))  # fmt: skip
        as_written = normalize(local) == local.strip().lower()
        level = ("L1" if b.how in ("exact", "alias") and as_written else
                 "L3" if b.how == "fuzzy" else "L2")  # fmt: skip
        levels[level] = LevelResult(cand, reason=f"terminology {b.how} match")
    elif fact.get("code_status") == "bound":  # bound earlier from text that no longer re-binds
        stored = f"{fact.get('code_system')} {fact.get('code')} (stored binding)"
        signals.append(Signal("terminology:stored", 2, cand, "supports",
                              fact.get("confidence_terminology"), stored))  # fmt: skip
        levels["L2"] = LevelResult(cand, reason="the stored terminology binding")
    elif fact["fact_type"] not in CONCEPT_FIELDS and cand:
        levels["L0"] = LevelResult(cand, reason="the field needs no concept: the reading itself")
    return signals, levels, cohort


def resolve_fact(conn: Connection, fact: dict[str, Any],
                 provenance: list[dict[str, Any]]) -> Decision:  # fmt: skip
    signals, levels, cohort = evidence_for(conn, fact, provenance)
    policy = load_policy()
    if get_settings().mlserve_backend == "stub":
        # Explicit synthetic baseline; this exemption must never activate for a real model.
        policy = replace(
            policy,
            validated=tuple(
                (level, group, field, "synthetic-stub-fixture" if basis == "assumed" else basis)
                for level, group, field, basis in policy.validated
            ),
        )
    d = resolve(signals, levels, field_=fact["fact_type"], cohort=cohort, policy=policy)
    d.trace[:0] = [{"step": "tier_not_built", **t} for t in _NOT_BUILT_TIERS]
    d.trace.insert(0, {"step": "input", "field": fact["fact_type"], "cohort": cohort,
                       "candidate": candidate_of(fact)})  # fmt: skip
    return d


def log_decision(conn: Connection, *, document_id: str, run_id: UUID | str | None, seq: int,
                 fact_id: UUID | str, decision: Decision,
                 confidence: float | None) -> None:  # fmt: skip
    """The decision trace as an E12-S5 agent step: each evidence source, its tier, score or
    reason, the level that resolved it or the cause of escalation."""
    conn.execute(
        text("INSERT INTO agent_trace (document_id, run_id, fact_id, seq, agent, step, tool, "
             "  input, output, confidence) "
             "VALUES (:d, :r, :f, :seq, :agent, 'resolve', :tool, CAST(:i AS jsonb), "
             "  CAST(:o AS jsonb), :c)"),
        {"d": document_id, "r": str(run_id) if run_id else None, "f": str(fact_id), "seq": seq,
         "agent": AGENT, "tool": decision.policy,
         "i": json.dumps({"matrix": decision.matrix}, default=str),
         "o": json.dumps({k: v for k, v in decision.as_json().items() if k != "matrix"},
                         default=str),
         "c": confidence},
    )  # fmt: skip


def latest_decision(conn: Connection, fact_id: UUID | str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT input, output, created_at FROM agent_trace WHERE fact_id = :f "
             "AND agent = :a ORDER BY id DESC LIMIT 1"), {"f": str(fact_id), "a": AGENT},
    ).mappings().first()  # fmt: skip
    if row is None:
        return None
    levels = {lv.id: lv.name for lv in load_policy().levels}
    out = {**row["output"], "matrix": row["input"].get("matrix", []), "at": row["created_at"]}
    out["level_name"] = levels.get(str(out.get("resolved_by_level")), "")
    return out
