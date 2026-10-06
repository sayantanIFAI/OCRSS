"""The canary against the incumbent, on the same live documents (backlog E10-S5).

The E2-S7 metrics (field-F1, numeric exactness, hallucination rate, FHIR validity) need gold labels,
so they are measured offline on the E2-S4 gold set and registered with each version
(``model_version.eval_report``); both reports are shown side by side here. Live, there is no gold
set: what stands in for it are the reviewers' own decisions and the pipeline's checks on the very
documents each side served, over the same window:

- ``partial_rate``: schema-invalid answers (``_partial``), the live schema-validity check
- ``failure_rate``: extractions that failed outright
- ``rejection_rate``: facts a reviewer rejected, of the facts reviewers decided (live hallucination)
- ``correction_rate``: facts a reviewer corrected, of those decided (live field errors)
- ``auto_accept_rate`` and ``mean_confidence``: how the S6 gate saw the answers
- extraction time p50 / p95

``verdict`` is the promotion gate: the canary may not be worse than the primary by more than
``LIMITS`` on any of them, over at least ``min_documents`` canary documents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from cdi_adapter.modelops import registry

LIMITS: dict[str, float] = {
    "min_documents": 20,  # canary documents before any verdict
    "failure_rate": 0.02,  # at most +2 points over the primary
    "partial_rate": 0.05,  # +5 points
    "rejection_rate": 0.05,  # +5 points, once both sides have min_reviewed decided facts
    "correction_rate": 0.10,  # +10 points, likewise
    "min_reviewed": 10,
    "eval_drop": 0.01,  # E2-S7 offline: field_f1 / numeric_exactness / fhir_validity may drop
    # by at most 1 point, hallucination_rate may rise by at most 1 point
}
EVAL_HIGHER_IS_BETTER = ("field_f1", "numeric_exactness", "fhir_validity")
EVAL_LOWER_IS_BETTER = ("hallucination_rate",)


def version_stats(conn: Connection, version: str, since: datetime | None) -> dict[str, Any]:
    p = {"v": version, "since": since}
    window = "AND created_at >= :since" if since is not None else ""
    ext = conn.execute(text(
        "SELECT count(*) AS docs, count(*) FILTER (WHERE is_partial) AS partial, "
        "coalesce(array_agg(DISTINCT model_name) FILTER (WHERE model_name IS NOT NULL), "
        f"'{{}}') AS served_by FROM extraction WHERE model_version = :v {window}"), p,
    ).mappings().one()  # fmt: skip
    run_window = "AND started_at >= :since" if since is not None else ""
    secs = "extract(epoch FROM finished_at - started_at)"
    runs = conn.execute(text(
        "SELECT count(*) FILTER (WHERE status = 'failed') AS failed, "
        "count(*) FILTER (WHERE status IN ('ok', 'failed')) AS finished, "
        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {secs}) "
        "FILTER (WHERE status = 'ok') AS p50, "
        f"percentile_cont(0.95) WITHIN GROUP (ORDER BY {secs}) "
        "FILTER (WHERE status = 'ok') AS p95 FROM pipeline_run WHERE stage = 'extract' "
        f"AND model_version = :v {run_window}"), p,
    ).mappings().one()  # fmt: skip
    fact_window = "AND e.created_at >= :since" if since is not None else ""
    facts = conn.execute(text(
        "SELECT count(*) AS facts, "
        "count(*) FILTER (WHERE f.review_state = 'auto_accepted') AS auto_accepted, "
        "count(*) FILTER (WHERE f.review_state IN ('clinician_confirmed', 'corrected', "
        "'rejected')) AS reviewed, "
        "count(*) FILTER (WHERE f.review_state = 'corrected') AS corrected, "
        "count(*) FILTER (WHERE f.review_state = 'rejected') AS rejected, "
        "avg(f.confidence_overall) AS mean_confidence "
        "FROM clinical_fact f JOIN extraction e ON e.id = f.extraction_id "
        f"WHERE e.model_version = :v AND f.supersedes IS NULL {fact_window}"), p,
    ).mappings().one()  # fmt: skip
    row = registry.get_version(conn, version) or {}

    def rate(n: Any, d: Any) -> float | None:
        return round(float(n) / float(d), 4) if d else None

    return {
        "version": version,
        "model_id": row.get("model_id"),
        "served_by": sorted(ext["served_by"]),
        "documents": ext["docs"],
        "partial_rate": rate(ext["partial"], ext["docs"]),
        "failure_rate": rate(runs["failed"], runs["finished"]),
        "facts": facts["facts"],
        "reviewed": facts["reviewed"],
        "rejection_rate": rate(facts["rejected"], facts["reviewed"]),
        "correction_rate": rate(facts["corrected"], facts["reviewed"]),
        "auto_accept_rate": rate(facts["auto_accepted"], facts["facts"]),
        "mean_confidence": (
            round(float(facts["mean_confidence"]), 4)
            if facts["mean_confidence"] is not None
            else None
        ),  # fmt: skip
        "extract_p50_s": round(float(runs["p50"]), 1) if runs["p50"] is not None else None,
        "extract_p95_s": round(float(runs["p95"]), 1) if runs["p95"] is not None else None,
        "eval_report": row.get("eval_report"),
    }


def verdict(canary: dict[str, Any], primary: dict[str, Any],
            limits: dict[str, float] | None = None) -> tuple[bool, list[str]]:  # fmt: skip
    """(may promote, reasons it may not). Pure: the numbers in, the decision out."""
    lim = {**LIMITS, **(limits or {})}
    why: list[str] = []
    if canary["documents"] < lim["min_documents"]:
        why.append(f"only {canary['documents']} canary documents; need {int(lim['min_documents'])}")
    for key in ("failure_rate", "partial_rate"):
        c, p = canary.get(key), primary.get(key)
        if c is not None and c > (p or 0.0) + lim[key]:
            why.append(f"{key} {c:.1%} vs primary {p or 0.0:.1%} (limit +{lim[key]:.0%})")
    if min(canary["reviewed"], primary["reviewed"]) >= lim["min_reviewed"]:
        for key in ("rejection_rate", "correction_rate"):
            c, p = canary.get(key), primary.get(key)
            if c is not None and p is not None and c > p + lim[key]:
                why.append(f"{key} {c:.1%} vs primary {p:.1%} (limit +{lim[key]:.0%})")
    ce, pe = canary.get("eval_report") or {}, primary.get("eval_report") or {}
    for key in EVAL_HIGHER_IS_BETTER:
        if isinstance(ce.get(key), int | float) and isinstance(pe.get(key), int | float):
            if ce[key] < pe[key] - lim["eval_drop"]:
                why.append(f"E2-S7 {key} {ce[key]} vs primary {pe[key]}")
    for key in EVAL_LOWER_IS_BETTER:
        if isinstance(ce.get(key), int | float) and isinstance(pe.get(key), int | float):
            if ce[key] > pe[key] + lim["eval_drop"]:
                why.append(f"E2-S7 {key} {ce[key]} vs primary {pe[key]}")
    return not why, why


def compare(conn: Connection) -> dict[str, Any]:
    """The live canary against the live primary, since the canary started taking traffic."""
    st = registry.current(conn)
    if st is None or st.canary_version is None:
        raise registry.RolloutError("no canary is deployed")
    since = registry.canary_started_at(conn)
    canary = version_stats(conn, st.canary_version, since)
    primary = version_stats(conn, st.primary_version, since)
    ok, why = verdict(canary, primary)
    notes = []
    if not canary["eval_report"] or not primary["eval_report"]:
        notes.append("no E2-S7 offline eval report on "
                     + " and ".join(v for v, r in ((st.canary_version, canary["eval_report"]),
                                                   (st.primary_version, primary["eval_report"]))
                                    if not r)
                     + ": the gold-set metrics are not compared")  # fmt: skip
    for side in (canary, primary):
        other = [m for m in side["served_by"] if m != side["model_id"]]
        if other:  # e.g. the 3B OOM fallback answered for a 7B version
            notes.append(f"{side['version']} was also served by {', '.join(other)}")
    return {"since": since.isoformat() if since else None, "canary_percent": st.canary_percent,
            "canary": canary, "primary": primary, "promote_ok": ok, "blocking": why,
            "notes": notes}  # fmt: skip
