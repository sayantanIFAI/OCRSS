"""S6 clinical validation + the governance gate (ARCHITECTURE §5 S6, §14 "The gate").

``validate_document(document_id)``:

1. Reads the fact set + the extraction row -> ``partial = payload._partial``.
2. Per fact: every rule, then ``_calibrate(conf, partial, n_warn, n_block)`` — an honest
   placeholder: subtract ``CDI_GATE_PARTIAL_PENALTY`` when the extraction was ``_partial``, cap at
   0.5 on any blocker, 0.9 on any warn (fitted isotonic calibration is backlog E4-S2).
3. **Gate -> review_state**:
   any blocker **or** ``_partial`` **or** conf < ``CDI_GATE_REVIEW_FLOOR`` **or** a medication
   with a ``med-*`` finding -> ``in_review``; conf >= ``CDI_GATE_AUTO_ACCEPT_CONF`` and zero
   findings -> ``auto_accepted``; conf >= ``CDI_GATE_AUDIT_CONF`` and zero blockers ->
   ``auto_accepted`` + an ``audit_sample_rate`` chance of a low-priority audit ``review_task``;
   otherwise ``in_review``.
4. Cross-document: same patient + ``fact_type`` + ``code`` -> a same-day different value is a
   ``fact_conflict(value_mismatch, CONTRADICTED, blocker)`` and both facts go ``in_review``; a
   same-day same value is ``fact_conflict(duplicate, SUPPORTED, info, auto_resolution=keep_a)``.
5. One ``review_task`` per document listing every held fact id, ``kind`` inferred
   (``dose_check`` / ``unmapped_terminology`` / ``low_confidence``); the payload carries findings.
6. ``pipeline_run(stage='validate')`` with counts; ``status='validated'``; audit.
7. An auto-accepted fact's evidence chain (backlog E4-S7): an ``interpretation_candidate`` and a
   ``verified_fact`` pointing at its raw observations (``cdi_adapter.evidence``).
8. The resolution cascade (backlog E4-S8, ``cdi_adapter.resolution``) on every gated fact: its
   evidence matrix, the level that resolved it or why it escalated, logged as an agent step. It
   can only hold a fact the gate would auto-accept (a tier-0/1 conflict, or no level validated for
   the field and cohort), never accept one the gate holds; the level and matrix are stored with
   the auto-accepted fact's candidate.
9. The fitted calibrator (backlog E4-S2, ``CDI_CALIBRATOR_VERSION``): when set, the gate
   decides on the isotonic-calibrated confidence of the fact's doc_type x fact_type (x field) slice
   instead of the fixed-penalty score, which stays in ``confidence_overall``; the note records
   both. Unset (the default) nothing changes.
   With ``CDI_GATE_THRESHOLDS`` too (backlog E4-S3, ``cdi_adapter.training.thresholds``), the
   derived per-class cut-offs replace the global constants for the classes they cover: a fact
   auto-accepts only when every field clears its threshold for the fact's evidence state.
10. Medication continuity across encounters (backlog E5-S1, ``cdi_adapter.longitudinal``): a
   governed medication reordered or changed at a later encounter supersedes the earlier order in
   the active list; the earlier order stays in the history.

Only machine states (``pending`` / ``auto_accepted`` / ``in_review``) are ever re-gated: a
clinician's decision (``clinician_confirmed`` / ``corrected`` / ``rejected``) is never overridden.
"""

from __future__ import annotations

import json
import random
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from cdi_adapter import evidence, fieldgrammar, longitudinal, metrics, repo, summary
from cdi_adapter.config import Settings, get_settings
from cdi_adapter.db import session_scope
from cdi_adapter.logging import get_logger
from cdi_adapter.resolution import facts as resolution
from cdi_adapter.training import calibrator, thresholds
from cdi_adapter.validate import rules
from cdi_adapter.validate.rules import Finding

log = get_logger(__name__)

HUMAN_STATES = ("clinician_confirmed", "corrected", "rejected")


def _calibrate(conf: float, partial: bool, n_warn: int, n_block: int,
               penalty: float | None = None) -> float:  # fmt: skip
    penalty = get_settings().gate_partial_penalty if penalty is None else penalty
    c = float(conf or 0.0)
    if partial:
        c -= penalty
    if n_block:
        c = min(c, 0.5)
    if n_warn:
        c = min(c, 0.9)
    return round(min(max(c, 0.0), 1.0), 4)


def audit_sampled(rate: float) -> bool:
    """§5 S6: an ``audit_sample_rate`` chance of a low-priority audit ``review_task``."""
    return random.random() < rate


def gate(fact_type: str, conf: float, findings: list[Finding], partial: bool,
         s: Settings) -> str:  # fmt: skip
    blockers = [f for f in findings if f.severity == "blocker"]
    med_findings = fact_type == "medication" and any(f.code.startswith("med-") for f in findings)
    if blockers or partial or conf < s.gate_review_floor or med_findings:
        return "in_review"
    if conf >= s.gate_auto_accept_conf and not findings:
        return "auto_accepted"
    if conf >= s.gate_audit_conf and not blockers:
        return "auto_accepted"
    return "in_review"


def _task_kind(held: list[dict[str, Any]]) -> str:
    codes = {f["code"] for h in held for f in h["findings"]}
    if any(c.startswith("med-") for c in codes):
        return "dose_check"
    if "unmapped-terminology" in codes:
        return "unmapped_terminology"
    return "low_confidence"


def _same_day(a: dict[str, Any], b: dict[str, Any]) -> bool:
    da = rules._as_date(a.get("effective_time")) or a.get("encounter_date")
    db = rules._as_date(b.get("effective_time")) or b.get("encounter_date")
    return da is not None and da == db


def _same_value(a: dict[str, Any], b: dict[str, Any]) -> bool:
    for col in ("value_num", "value_text", "value_code"):
        if a.get(col) is not None or b.get(col) is not None:
            va, vb = a.get(col), b.get(col)
            if isinstance(va, int | float) or isinstance(vb, int | float):
                return va is not None and vb is not None and abs(float(va) - float(vb)) < 1e-9
            return str(va or "").strip().lower() == str(vb or "").strip().lower()
    return True  # no value (e.g. a condition): same code is the same assertion


def find_similar_current_facts(conn: Connection, fact: dict[str, Any],
                               document_id: str) -> list[dict[str, Any]]:  # fmt: skip
    rows = conn.execute(
        text(
            "SELECT f.*, e.period_start AS encounter_date FROM clinical_fact f "
            "LEFT JOIN encounter e ON e.id = f.encounter_id "
            "WHERE f.patient_id = :p AND f.fact_type = :t AND f.code = :c AND f.is_current "
            "AND f.review_state <> 'rejected' AND NOT (:d = ANY(f.source_doc_ids))"
        ),
        {"p": fact["patient_id"], "t": fact["fact_type"], "c": fact["code"], "d": document_id},
    ).mappings()
    return [dict(r) for r in rows]


def _set_state(conn: Connection, fact_id, state: str, conf: float | None,
               note: dict[str, Any] | None) -> None:  # fmt: skip
    conn.execute(
        text(
            "UPDATE clinical_fact SET review_state = :s, "
            "confidence_overall = coalesce(:c, confidence_overall), "
            "review_note = coalesce(CAST(:n AS jsonb), review_note), updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": fact_id, "s": state, "c": conf, "n": json.dumps(note) if note else None},
    )


def validate_document(document_id: UUID | str, *, actor: str = "system") -> dict[str, Any]:
    s = get_settings()
    doc_id = str(document_id)
    with session_scope() as conn:
        run_id = repo.start_run(conn, document_id=doc_id, stage="validate", model_name="rules")
    try:
        with session_scope() as conn:
            counts = _validate(conn, doc_id, s, run_id)
            repo.set_document_status(conn, doc_id, "validated")
            repo.finish_run(conn, run_id, metrics=counts)
            repo.write_audit(conn, actor=actor, action="update", entity="source_document",
                             entity_id=doc_id, detail={"stage": "validate", **counts})  # fmt: skip
    except Exception as exc:
        with session_scope() as conn:
            repo.fail_run(conn, run_id, error=f"{type(exc).__name__}: {exc}")
            err = {"stage": "validate", "message": str(exc)}
            repo.set_document_status(conn, doc_id, "error", error=err)
        raise
    log.info("validated", document_id=doc_id, **counts)
    return counts


def _validate(conn: Connection, doc_id: str, s: Settings,
              run_id: UUID | str | None = None) -> dict[str, Any]:  # fmt: skip
    ext = conn.execute(
        text("SELECT payload FROM extraction WHERE document_id = :d "
             "ORDER BY created_at DESC LIMIT 1"), {"d": doc_id}
    ).scalar_one_or_none()  # fmt: skip
    partial = bool((ext or {}).get("_partial"))
    enc_date: date | None = conn.execute(
        text("SELECT period_start FROM encounter WHERE source_document_id = :d LIMIT 1"),
        {"d": doc_id},
    ).scalar_one_or_none()

    # idempotent re-run: this document's open tasks and conflicts are recomputed
    conn.execute(text("DELETE FROM review_task WHERE document_id = :d AND status = 'open'"),
                 {"d": doc_id})  # fmt: skip
    conn.execute(
        text("DELETE FROM fact_conflict WHERE fact_a IN (SELECT id FROM clinical_fact "
             "WHERE :d = ANY(source_doc_ids)) OR fact_b IN (SELECT id FROM clinical_fact "
             "WHERE :d = ANY(source_doc_ids))"), {"d": doc_id}
    )  # fmt: skip

    facts = repo.get_facts_for_document(conn, doc_id)
    # E15-S2: the engine's own per-field readings, to judge against the clinical field grammars
    med_readings = fieldgrammar.readings_by_drug(ext)
    cutoffs = thresholds.check_config(s.gate_thresholds, s.calibrator_version)  # E4-S3
    cls = repo.get_latest_classification(conn, doc_id) if s.calibrator_version else None
    doc_type = cls["doc_type"] if cls else None
    held: list[dict[str, Any]] = []
    audit: list[str] = []
    n_auto = n_conflict = 0
    confidences: list[tuple[str, float]] = []  # E10-S4: observed once the gate pass is done
    for f in facts:
        if f["review_state"] in HUMAN_STATES:
            continue
        f["encounter_date"] = enc_date
        range_findings, updates = rules.check_value_range(f)
        if updates:
            f.update(updates)
            conn.execute(text("UPDATE clinical_fact SET value_num = :value_num, "
                              "value_unit_ucum = :value_unit_ucum WHERE id = :id"),
                         {"id": f["id"], **updates})  # fmt: skip
        provenance = repo.get_provenance(conn, f["id"])
        findings = (range_findings + rules.check_unit_present(f) + rules.check_medication(f)
                    + rules.check_dates(f, enc_date)
                    + rules.check_evidence(provenance)
                    + rules.check_terminology(f)
                    + rules.check_field_grammars(f, med_readings.get(
                        fieldgrammar.normalise(f.get("drug_text")).lower())))  # fmt: skip

        # cross-document duplicate / contradiction
        if f.get("code"):
            for other in find_similar_current_facts(conn, f, doc_id):
                if not _same_day(f, other):
                    continue
                if _same_value(f, other):
                    _conflict(conn, f, other, "duplicate", "SUPPORTED", "info", "keep_a")
                    findings.append(Finding("info", "duplicate", f"same as fact {other['id']}"))
                else:
                    n_conflict += 1
                    _conflict(conn, f, other, "value_mismatch", "CONTRADICTED", "blocker", None)
                    msg = f"same-day different value in fact {other['id']}"
                    findings.append(Finding("blocker", "contradiction", msg))
                    # §5 S6: "both facts forced in_review"
                    back = Finding("blocker", "contradiction", f"conflicts with fact {f['id']}")
                    _set_state(conn, other["id"], "in_review", None,
                               {"findings": [back.as_json()]})  # fmt: skip

        n_warn = sum(x.severity == "warn" for x in findings)
        n_block = sum(x.severity == "blocker" for x in findings)
        conf = _calibrate(f.get("confidence_overall") or 0.0, partial, n_warn, n_block,
                          s.gate_partial_penalty)  # fmt: skip
        # E4-S2: the fitted calibrator when one is named; the fixed caps still bound it
        score, fitted = calibrator.gate_score(s.calibrator_version, s.database_url, doc_type,
                                              f["fact_type"], conf)  # fmt: skip
        if fitted is not None and score != conf:
            score = min(score, 0.5) if n_block else min(score, 0.9) if n_warn else score
        gate_findings = [x for x in findings if x.severity != "info"]
        state = gate(f["fact_type"], score, gate_findings, partial, s)
        decision = resolution.resolve_fact(conn, f, provenance)  # E4-S8: stricter only
        by_class = None
        if cutoffs is not None:
            by_rules = partial or any(x.severity in ("blocker", "warn") for x in gate_findings)
            present_fields = {
                name
                for name, _ in calibrator.field_outcomes(
                    {
                        "fact_type": f["fact_type"],
                        "proposed": f,
                        "corrected": f,
                        "decision": "confirmed",
                    }
                )
            }
            by_rules = by_rules or not present_fields <= (fitted or {}).get("fields", {}).keys()
            t_state, by_class = thresholds.gate_decision(
                cutoffs, doc_type, f["fact_type"], (fitted or {}).get("fields", {}),
                thresholds.evidence_state(decision.matrix), by_rules)  # fmt: skip
            if t_state is not None:
                state = t_state
        if state == "auto_accepted" and decision.outcome == "review":
            held_by = Finding("blocker", "cascade-review", decision.escalation_cause or "")
            findings.append(held_by)
            gate_findings.append(held_by)
            state = "in_review"
        resolution.log_decision(conn, document_id=doc_id, run_id=run_id, seq=len(confidences),
                                fact_id=f["id"], decision=decision, confidence=score)  # fmt: skip
        confidences.append((f["fact_type"], conf))
        note = {"findings": [x.as_json() for x in findings], "calibrated": score,
                "partial": partial,
                "resolution": {"level": decision.resolved_by_level, "outcome": decision.outcome,
                               "cause": decision.escalation_cause,
                               "policy": decision.policy}}  # fmt: skip
        if fitted is not None:
            note["calibrator"] = fitted
        if by_class is not None:
            note["thresholds"] = by_class
        _set_state(conn, f["id"], state, conf, note)
        if state == "in_review":
            held.append({"id": str(f["id"]), "findings": [x.as_json() for x in gate_findings]})
        else:
            n_auto += 1
            evidence.record_auto_accept(conn, f, provenance, confidence=score,
                                        policy=evidence.policy_id(s),
                                        decision=decision)  # fmt: skip
            if gate_findings or score < s.gate_auto_accept_conf:
                if audit_sampled(s.audit_sample_rate):
                    audit.append(str(f["id"]))

    patient_id = facts[0]["patient_id"] if facts else None
    longitudinal.reconcile_patient(conn, patient_id)  # E5-S1: medications + problems
    # E5-S2: and the derived summary, on this connection and in this transaction - so it is
    # already current when the job projects FHIR in the same window (§8.1). No refresh step.
    summary.refresh_patient_summary(conn, patient_id)
    if held:
        _task(conn, doc_id, patient_id, _task_kind(held), "normal", held)
    if audit:
        _task(conn, doc_id, patient_id, "audit_sample", "low",
              [{"id": a, "findings": []} for a in audit])  # fmt: skip
    for fact_type, c in confidences:  # E10-S4 drift input
        metrics.FACT_CONFIDENCE.labels(metrics.fact_type_label(fact_type)).observe(c)
    metrics.GATE_DECISIONS.labels("auto_accepted").inc(n_auto)
    metrics.GATE_DECISIONS.labels("in_review").inc(len(held))
    return {"facts": len(facts), "auto_accepted": n_auto, "in_review": len(held),
            "audit_sampled": len(audit), "conflicts": n_conflict, "partial": partial}  # fmt: skip


def _conflict(conn, a, b, kind, state, severity, resolution) -> None:
    conn.execute(
        text(
            "INSERT INTO fact_conflict (patient_id, fact_a, fact_b, conflict_type, evidence_state, "
            "  severity, auto_resolution) VALUES (:p, :a, :b, :k, :e, :s, :r)"
        ),
        {"p": a["patient_id"], "a": a["id"], "b": b["id"], "k": kind, "e": state,
         "s": severity, "r": resolution},
    )  # fmt: skip


def _task(conn, doc_id, patient_id, kind, priority, items) -> None:
    conn.execute(
        text(
            "INSERT INTO review_task (document_id, patient_id, kind, priority, fact_ids, payload) "
            "VALUES (:d, :p, :k, :pr, CAST(:ids AS uuid[]), CAST(:payload AS jsonb))"
        ),
        {"d": doc_id, "p": patient_id, "k": kind, "pr": priority,
         "ids": [i["id"] for i in items], "payload": json.dumps({"facts": items})},
    )  # fmt: skip
