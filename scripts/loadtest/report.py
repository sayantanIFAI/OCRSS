"""Latency report for the load test (backlog E1-S7): exact percentiles, histogram, SLA verdict.

The SLA (ADR-0002, infra backlog §2.1): from the last upload to governed JSON/FHIR **or an explicit
hold with an ETA** within 5 minutes. A job that stops at ``state = review`` (facts held for the
reviewer, §7.2) is that explicit hold, so it counts as ready, but only if none of its documents
failed: a failed document is neither governed nor held. ``mismatch`` is an identity guard outcome,
reported separately. ``error``, ``documents_failed``, a timeout or a failed request is a failure and
never counts as within the SLA.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

READY_STATES = frozenset({"review"})  # the job stops for human review, never auto-projects


@dataclass(frozen=True)
class Sample:
    patient: int
    documents: int
    upload_s: float  # time to send the files (POST /api/jobs)
    ready_s: float | None  # last upload -> terminal state + bundles fetched; None = never ready
    state: str  # review | mismatch | error | documents_failed | timeout | request_failed
    detail: str = ""
    docs_failed: int = 0  # documents the job reported with status "error"

    @property
    def ok(self) -> bool:
        return self.state in READY_STATES and self.ready_s is not None and not self.docs_failed


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the value at rank ceil(q/100 * n), exact, no interpolation."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def histogram(values: Iterable[float], bin_s: float, sla_s: float) -> list[dict[str, Any]]:
    """Fixed-width bins from 0 to past the slowest sample (and at least to 2x the SLA)."""
    if not math.isfinite(bin_s) or bin_s <= 0 or not math.isfinite(sla_s) or sla_s <= 0:
        raise ValueError("bin_s and sla_s must be finite and positive")
    vals = list(values)
    if any(not math.isfinite(v) or v < 0 for v in vals):
        raise ValueError("latencies must be finite and nonnegative")
    top = max([2 * sla_s, *vals]) if vals else 2 * sla_s
    bins = []
    edge = 0.0
    while edge < top or (vals and edge == max(vals)):
        hi = edge + bin_s
        count = sum(1 for v in vals if edge <= v < hi)
        bins.append({"from_s": edge, "to_s": hi, "count": count, "within_sla": hi <= sla_s})
        edge = hi
    return bins


def build(
    samples: Sequence[Sample], *, sla_s: float, bin_s: float, target: Mapping[str, Any]
) -> dict[str, Any]:
    ready = [s.ready_s for s in samples if s.ok and s.ready_s is not None]
    states: dict[str, int] = {}
    for s in samples:
        states[s.state] = states.get(s.state, 0) + 1
    within = sum(1 for v in ready if v <= sla_s)
    pct = {f"p{q}": round(percentile(ready, q), 3) for q in (50, 95, 99)} if ready else {}
    return {
        "target": dict(target),
        "sla_s": sla_s,
        "patients": len(samples),
        "documents": sum(s.documents for s in samples),
        "documents_failed": sum(s.docs_failed for s in samples),
        "states": states,
        "ready": len(ready),
        "within_sla": within,
        "within_sla_fraction": round(within / len(samples), 4) if samples else 0.0,
        "latency_s": {**pct, "min": min(ready, default=None), "max": max(ready, default=None)},
        "upload_s": {
            f"p{q}": round(percentile([s.upload_s for s in samples], q), 3) for q in (50, 95)
        }
        if samples
        else {},
        # the test passes only if every patient is ready and p95 is inside the SLA
        "sla_met": bool(samples)
        and len(ready) == len(samples)
        and pct.get("p95", math.inf) <= sla_s,
        "histogram": histogram(ready, bin_s, sla_s),
        "samples": [asdict(s) for s in samples],
    }


def render_text(report: Mapping[str, Any]) -> str:
    lat = report["latency_s"]
    lines = [
        f"CDI load test: {report['patients']} patients, {report['documents']} documents "
        f"-> {report['target'].get('host', '?')}",
        f"states: {', '.join(f'{k}={v}' for k, v in sorted(report['states'].items()))}"
        f"   failed documents: {report['documents_failed']}",
        "upload -> bundle-ready: "
        + (
            f"p50 {lat['p50']:.1f}s  p95 {lat['p95']:.1f}s  p99 {lat['p99']:.1f}s  "
            f"max {lat['max']:.1f}s"
            if "p50" in lat
            else "no patient reached a ready state"
        ),
        f"within the {report['sla_s']:g}s SLA: {report['within_sla']}/{report['patients']} "
        f"-> SLA {'MET' if report['sla_met'] else 'NOT MET'}",
        "",
    ]
    peak = max((b["count"] for b in report["histogram"]), default=0) or 1
    for b in report["histogram"]:
        bar = "#" * math.ceil(40 * b["count"] / peak) if b["count"] else ""
        mark = " " if b["within_sla"] else "!"
        lines.append(f"{mark}{b['from_s']:6.0f}-{b['to_s']:<6.0f}s {b['count']:4d} {bar}")
    lines.append(f"  (! = beyond the {report['sla_s']:g}s SLA)")
    return "\n".join(lines) + "\n"


def dump(report: Mapping[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
