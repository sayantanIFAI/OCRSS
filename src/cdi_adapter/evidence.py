"""The evidence chain (backlog E4-S7, ARCHITECTURE §15.4): observation -> candidate -> verified.

Written by the two places a fact is verified; nothing else changes:

* the S6 gate (``validate/service.py``): a fact the gate auto-accepts gets an
  ``interpretation_candidate`` (source ``terminology`` when a standard code is bound, else
  ``extractor``) and a ``verified_fact`` (method ``auto_gate``, the calibrated confidence, the
  gate's thresholds as ``policy_id``, the model stack that proposed it);
* the S8 decision (``webapp/review.py`` ``decide``, used by every reviewer path): a
  ``reviewer`` candidate and a ``verified_fact`` (method ``reviewer``, the reviewer's id) for the
  fact version the decision produced - a correction's new version, or the accepted / rejected one.

Both tables are append-only in the database (migration 0013), so a later re-extraction, which
rewrites ``clinical_fact``, never removes a verification or a reviewer's decision.
``trace(fact_id)`` walks a verified fact back to its raw observations, their pixels (the page
image, the bbox, the crop's hash) and the engine and model versions.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter.config import Settings
from cdi_adapter.resolution.cascade import Decision


def policy_id(s: Settings) -> str:
    """The S6 gate policy a machine verification was made under (fixed thresholds until E4-S3),
    and the fitted calibrator when one was used (E4-S2)."""
    cal = f";calibrator=v{s.calibrator_version}" if s.calibrator_version else ""
    if s.gate_thresholds:
        from cdi_adapter.training.thresholds import load_config

        cal += f";thresholds=v{load_config(s.gate_thresholds).version}"
    return (f"s6-gate:auto>={s.gate_auto_accept_conf};audit>={s.gate_audit_conf};"
            f"floor={s.gate_review_floor}{cal}")  # fmt: skip


def observation_ids(provenance: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for p in provenance:
        for b in p.get("ocr_block_ids") or []:
            seen.setdefault(str(b), None)
    return list(seen)


def model_version(provenance: list[dict[str, Any]]) -> str | None:
    """The model stack that proposed the value: the first machine provenance row's."""
    for p in provenance:
        stack = p.get("model_stack") or {}
        if stack and "reviewer" not in stack:
            return json.dumps(stack, sort_keys=True, default=str)
    return None


def _value(f: dict[str, Any]) -> dict[str, Any]:
    return {"value_text": f.get("value_text") or f.get("local_text"),
            "value_num": f.get("value_num"), "unit": f.get("value_unit_ucum"),
            "code_system": f.get("code_system"), "code": f.get("code")}  # fmt: skip


def _candidate(conn: Connection, f: dict[str, Any], obs: list[str], *, source: str,
               level: str | None, score: float | None, evidence: dict[str, Any],
               model: str | None, created_by: str) -> UUID:  # fmt: skip
    v = _value(f)
    return conn.execute(
        text("INSERT INTO interpretation_candidate (document_id, fact_id, observation_ids, field, "
             "  value_text, value_num, unit, concept, code_system, code, source, "
             "  resolved_by_level, score, evidence, model_version, created_by) "
             "VALUES (:d, :f, CAST(:o AS uuid[]), :field, :vt, :vn, :u, :concept, :cs, :c, :src, "
             "  :lvl, :score, CAST(:ev AS jsonb), :m, :by) RETURNING id"),
        {"d": _document(f), "f": str(f["id"]), "o": obs, "field": f["fact_type"],
         "vt": v["value_text"], "vn": v["value_num"], "u": v["unit"],
         "concept": f.get("code_display"), "cs": v["code_system"], "c": v["code"], "src": source,
         "lvl": level, "score": score, "ev": json.dumps(evidence, default=str), "m": model,
         "by": created_by},
    ).scalar_one()  # fmt: skip


def _verified(conn: Connection, f: dict[str, Any], candidate: UUID, obs: list[str], *,
              method: str, confidence: float | None, policy: str | None, reviewer: str | None,
              model: str | None) -> UUID:  # fmt: skip
    v = _value(f)
    return conn.execute(
        text("INSERT INTO verified_fact (fact_id, document_id, candidate_id, observation_ids, "
             "  fact_type, value_text, value_num, unit, code_system, code, review_state, "
             "  verification_method, confidence, policy_id, reviewer_id, model_version) "
             "VALUES (:f, :d, :cand, CAST(:o AS uuid[]), :t, :vt, :vn, :u, :cs, :c, :state, "
             "  :method, :conf, :policy, :rev, :m) RETURNING id"),
        {"f": str(f["id"]), "d": _document(f), "cand": str(candidate), "o": obs,
         "t": f["fact_type"], "vt": v["value_text"], "vn": v["value_num"], "u": v["unit"],
         "cs": v["code_system"], "c": v["code"], "state": f["review_state"], "method": method,
         "conf": confidence, "policy": policy, "rev": reviewer, "m": model},
    ).scalar_one()  # fmt: skip


def _document(f: dict[str, Any]) -> str | None:
    docs = f.get("source_doc_ids") or []
    return str(docs[0]) if docs else None


def record_auto_accept(conn: Connection, fact: dict[str, Any], provenance: list[dict[str, Any]],
                       *, confidence: float, policy: str,
                       decision: Decision | None = None) -> UUID | None:  # fmt: skip
    """The S6 gate auto-accepted ``fact`` (its state already written). Once per fact version: a
    re-run of the gate that keeps it auto-accepted records nothing new. ``decision`` (the E4-S8
    cascade) gives the level that resolved it and its per-signal evidence matrix."""
    done = conn.execute(
        text("SELECT 1 FROM verified_fact WHERE fact_id = :f AND verification_method = 'auto_gate' "
             "LIMIT 1"), {"f": str(fact["id"])},
    ).first()  # fmt: skip
    if done:
        return None
    obs = observation_ids(provenance)
    bound = fact.get("code_status") == "bound"
    model = model_version(provenance)
    evidence = {"confidence_ocr": fact.get("confidence_ocr"),
                "confidence_extract": fact.get("confidence_extract"),
                "confidence_terminology": fact.get("confidence_terminology"),
                "code_status": fact.get("code_status"), "gate_confidence": confidence,
                "observations": len(obs)}  # fmt: skip
    level = "S5 terminology binding" if bound else "S4 extraction"
    if decision is not None:
        level = decision.resolved_by_level
        evidence |= {"matrix": decision.matrix, "cascade": decision.policy}
    cand = _candidate(conn, fact, obs, source="terminology" if bound else "extractor",
                      level=level, score=confidence, evidence=evidence, model=model,
                      created_by="system")  # fmt: skip
    return _verified(conn, {**fact, "review_state": "auto_accepted"}, cand, obs,
                     method="auto_gate", confidence=confidence, policy=policy, reviewer=None,
                     model=model)  # fmt: skip


def record_reviewer_decision(conn: Connection, fact_id: UUID | str, action: str,
                             reviewer: str, prior_fact_id: UUID | str) -> UUID:  # fmt: skip
    """A reviewer's decision on a fact: a ``reviewer`` candidate (the value as decided) and a
    ``verified_fact`` for the fact version it produced."""
    f = dict(conn.execute(text("SELECT * FROM clinical_fact WHERE id = :f"),
                          {"f": str(fact_id)}).mappings().one())  # fmt: skip
    prov = [dict(r) for r in conn.execute(
        text("SELECT * FROM fact_provenance WHERE fact_id = :f ORDER BY recorded_at"),
        {"f": str(fact_id)}).mappings()]  # fmt: skip
    obs = observation_ids(prov)
    cand = _candidate(conn, f, obs, source="reviewer", level="L8", score=None,
                      evidence={"action": action, "prior_fact_id": str(prior_fact_id)},
                      model=None, created_by=reviewer)  # fmt: skip
    return _verified(conn, f, cand, obs, method="reviewer", confidence=None, policy=None,
                     reviewer=reviewer, model=None)  # fmt: skip


def trace(conn: Connection, fact_id: UUID | str) -> list[dict[str, Any]]:
    """Every verification of a fact, each walked back to its candidate, its raw observations
    (superseded ones included), the page image they were read from and the OCR run's model."""
    out = []
    for v in conn.execute(
        text("SELECT * FROM verified_fact WHERE fact_id = :f ORDER BY created_at"),
        {"f": str(fact_id)},
    ).mappings():
        cand = conn.execute(text("SELECT * FROM interpretation_candidate WHERE id = :c"),
                            {"c": v["candidate_id"]}).mappings().one()  # fmt: skip
        obs = conn.execute(
            text("SELECT o.id, o.raw_text, o.bbox, o.crop_hash, o.engine, o.engine_version, "
                 "  o.prompt_hash, o.region_kind, o.ocr_conf, o.superseded_by, "
                 "  p.page_no, p.storage_key AS page_image, r.model_name AS run_model "
                 "FROM ocr_observation o JOIN document_page p ON p.id = o.page_id "
                 "LEFT JOIN pipeline_run r ON r.id = o.pipeline_run_id "
                 "WHERE o.id = ANY(CAST(:ids AS uuid[])) ORDER BY p.page_no, o.reading_order"),
            {"ids": [str(i) for i in v["observation_ids"]]},
        ).mappings()  # fmt: skip
        out.append({"verified_fact": dict(v), "candidate": dict(cand),
                    "observations": [dict(o) for o in obs]})  # fmt: skip
    return out
