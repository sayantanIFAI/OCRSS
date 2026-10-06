"""Isotonic confidence calibrator per doc_type x fact_type (field) (backlog E4-S2, §15.10).

``fit_calibrator`` learns, from a published E4-S1 training version, how often a fact the gate
scored ``x`` was actually right, and stores the curve per slice:

* **slices**: (doc_type, fact_type, field). For prescription medications the unit is the FIELD -
  medicine, strength, dose, frequency, duration; lab orders ``lab_test``; conditions
  ``diagnosis``; every other fact type is one ``value``. A field is right when the reviewer's
  final value equals the proposal (a rejection makes every proposed field wrong).
* **held-out reliability**: 1 in 5 document groups per field (a stable hash; legacy rows
  without a document use fact id)
  is kept out of the fit; the reliability diagram (predicted vs observed correctness in 10 bins)
  and the expected calibration error before and after are measured on them.
* a slice with fewer than ``min_examples`` training examples is ``insufficient_data``: it is never
  guessed, and the gate keeps the fixed-penalty score there.
* the **tolerance** is a placeholder until the clinical owner agrees the band (the AC says
  "agreed"); it is stored with its basis.

``gate_score`` is what the S6 gate calls: with ``CDI_CALIBRATOR_VERSION`` unset (the default) it
returns the fixed-penalty ``_calibrate`` score unchanged; set, it returns the calibrated value -
for a field-level fact the lowest of its fields' (the most conservative) - and what it used.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.catalogue.normalize import split_name
from cdi_adapter.terminology.service import normalize
from cdi_adapter.training import calibration
from cdi_adapter.training.isotonic import Curve, fit

METHOD = "isotonic_pav_v2"
DEFAULT_MIN_EXAMPLES = 30
DEFAULT_TOLERANCE = 0.05
TOLERANCE_BASIS = "placeholder: the clinical owner agrees the band (backlog E4-S2)"
MIN_TEST_FOR_VERDICT = 10
BINS = 10
FIELDS: dict[str, tuple[str, ...]] = {
    "medication": ("medicine", "strength", "dose", "frequency", "duration"),
    "lab_order": ("lab_test",),
    "condition": ("diagnosis",),
}

Key = tuple[str, str, str]  # (doc_type, fact_type, field)


# ------------------------------------------------------------------ per-field correctness


def _num(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _field(fact_type: str, field: str, v: dict[str, Any] | None) -> Any:
    """The comparable value of one field of a proposed / corrected fact (None: not written)."""
    if v is None:
        return None
    if field == "medicine":
        if v.get("code"):
            return ("code", v.get("code_system"), v["code"])
        name = split_name(v.get("drug_text") or v.get("local_text"))[0]
        return ("name", name) if name else None
    if field == "strength":
        n = _num(v.get("strength_num"))
        return None if n is None else (n, str(v.get("strength_unit") or "").lower())
    if field == "dose":
        n = _num(v.get("dose_num"))
        return None if n is None else (n, str(v.get("dose_unit_ucum") or "").lower())
    if field == "duration":
        return _num(v.get("duration_days"))
    if field == "frequency":
        f = str(v.get("frequency_code") or "").strip().lower()
        return f or None
    if field in ("lab_test", "diagnosis"):
        if v.get("code"):
            return ("code", v.get("code_system"), v["code"])
        t = normalize(v.get("local_text"))
        return ("text", t) if t else None
    return json.dumps(v, sort_keys=True, default=str)  # "value": the whole fact


def field_outcomes(example: dict[str, Any]) -> list[tuple[str, int]]:
    """(field, 1 when the machine's value was right else 0) for each field the fact has."""
    ft = example["fact_type"]
    proposed, corrected = example["proposed"], example.get("corrected")
    out = []
    for field in FIELDS.get(ft, ("value",)):
        p = _field(ft, field, proposed)
        if example["decision"] == "rejected":
            if p is not None:
                out.append((field, 0))
            continue
        c = _field(ft, field, corrected)
        if p is None and c is None:
            continue  # neither side has this field
        out.append((field, int(p == c)))
    return out


def held_out(fact_id: Any, field: str) -> bool:
    return hashlib.sha256(f"{fact_id}:{field}".encode()).digest()[0] % 5 == 0


# ------------------------------------------------------------------ reliability


def reliability(pred: Iterable[float], y: Iterable[int], bins: int = BINS) -> dict[str, Any]:
    pairs = list(zip(pred, y, strict=True))
    out = []
    ece = gap = 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        inside = [(p, o) for p, o in pairs if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if not inside:
            continue
        mp = sum(p for p, _ in inside) / len(inside)
        ob = sum(o for _, o in inside) / len(inside)
        out.append({"lo": lo, "hi": hi, "n": len(inside), "mean_predicted": round(mp, 4),
                    "observed": round(ob, 4)})  # fmt: skip
        ece += len(inside) / len(pairs) * abs(mp - ob)
        gap = max(gap, abs(mp - ob))
    return {"bins": out, "ece": round(ece, 4), "max_gap": round(gap, 4), "n": len(pairs)}


# ------------------------------------------------------------------ fit


def slice_examples(rows: list[dict[str, Any]]) -> dict[Key, list[tuple[float, int, bool]]]:
    """(score, correct, held out) per slice; an example without a score cannot be used."""
    out: dict[Key, list[tuple[float, int, bool]]] = defaultdict(list)
    for r in rows:
        if r.get("confidence") is None:
            continue
        if not math.isfinite(float(r["confidence"])) or not 0 <= float(r["confidence"]) <= 1:
            raise ValueError("training confidence must be a finite probability")
        for field, ok in field_outcomes(r):
            key = (r.get("doc_type") or "unknown", r["fact_type"], field)
            out[key].append(
                (
                    float(r["confidence"]),
                    ok,
                    held_out(r.get("document_id") or r["proposed_fact_id"], field),
                )
            )
    return out


def fit_calibrator(conn: Connection, dataset_version: int | None = None, *,
                   min_examples: int = DEFAULT_MIN_EXAMPLES,
                   tolerance: float = DEFAULT_TOLERANCE,
                   actor: str = "calibrator-fit") -> dict[str, Any]:  # fmt: skip
    if min_examples < 1 or not math.isfinite(tolerance) or not 0 <= tolerance <= 1:
        raise ValueError("positive min_examples and a finite tolerance in [0,1] are required")
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('calibrator_fit'))"))
    if dataset_version is None:
        dataset_version = conn.execute(
            text("SELECT max(version) FROM calibration_dataset")).scalar_one()  # fmt: skip
        if dataset_version is None:
            raise LookupError("no published training dataset: run the E4-S1 export first")
    rows = calibration.load(conn, dataset_version)
    version = (conn.execute(text("SELECT max(version) FROM calibration_model")).scalar_one()
               or 0) + 1  # fmt: skip
    slices = []
    for key, ex in sorted(slice_examples(rows).items()):
        train = [(x, y) for x, y, h in ex if not h]
        test = [(x, y) for x, y, h in ex if h]
        s: dict[str, Any] = {"doc_type": key[0], "fact_type": key[1], "field": key[2],
                             "n_train": len(train), "n_test": len(test), "curve": None,
                             "reliability": None, "ece_before": None, "ece_after": None,
                             "within_tolerance": None}  # fmt: skip
        if len(train) < min_examples:
            s["status"] = "insufficient_data"
        else:
            curve = fit([x for x, _ in train], [y for _, y in train])
            evaluated_on = test if test else train
            xs, ys = [x for x, _ in evaluated_on], [y for _, y in evaluated_on]
            after = reliability([curve.predict(x) for x in xs], ys)
            before = reliability(xs, ys)
            s |= {"status": "fitted", "curve": curve.as_json(),
                  "reliability": {"after": after, "before": before,
                                  "evaluated_on": "held_out" if test else "training"},
                  "ece_before": before["ece"], "ece_after": after["ece"],
                  "within_tolerance": (after["ece"] <= tolerance
                                       if len(test) >= MIN_TEST_FOR_VERDICT else None)}  # fmt: skip
        slices.append(s)
    fitted = [s for s in slices if s["status"] == "fitted"]
    summary = {"slices": len(slices), "fitted": len(fitted),
               "insufficient_data": len(slices) - len(fitted),
               "within_tolerance": sum(bool(s["within_tolerance"]) for s in fitted),
               "examples": len(rows)}  # fmt: skip
    model_id = conn.execute(
        text("INSERT INTO calibration_model (version, dataset_version, method, min_examples, "
             "  tolerance, tolerance_basis, summary, created_by) "
             "VALUES (:v, :d, :m, :me, :t, :tb, CAST(:s AS jsonb), :by) RETURNING id"),
        {"v": version, "d": dataset_version, "m": METHOD, "me": min_examples, "t": tolerance,
         "tb": TOLERANCE_BASIS, "s": json.dumps(summary), "by": actor},
    ).scalar_one()  # fmt: skip
    for s in slices:
        conn.execute(
            text("INSERT INTO calibration_slice (model_id, doc_type, fact_type, field, status, "
                 "  n_train, n_test, curve, reliability, ece_before, ece_after, "
                 "  within_tolerance) "
                 "VALUES (:m, :doc_type, :fact_type, :field, :status, :n_train, :n_test, "
                 "  CAST(:curve AS jsonb), CAST(:rel AS jsonb), :ece_before, :ece_after, "
                 "  :within_tolerance)"),
            {**s, "m": str(model_id), "curve": json.dumps(s["curve"]) if s["curve"] else None,
             "rel": json.dumps(s["reliability"]) if s["reliability"] else None},
        )  # fmt: skip
    repo.write_audit(conn, actor=actor, action="create", entity="calibration_model",
                     entity_id=str(model_id),
                     detail={"version": version, "dataset_version": dataset_version,
                             **summary})  # fmt: skip
    return {"version": version, "dataset_version": dataset_version, **summary}


# ------------------------------------------------------------------ use at the gate


def curves_for(conn: Connection, version: int) -> dict[Key, Curve]:
    """A fitted calibrator's curves (the fitted slices only)."""
    rows = conn.execute(
        text("SELECT s.doc_type, s.fact_type, s.field, s.curve FROM calibration_slice s "
             "JOIN calibration_model m ON m.id = s.model_id "
             "WHERE m.version = :v AND s.status = 'fitted'"), {"v": version},
    ).all()  # fmt: skip
    return {(r.doc_type, r.fact_type, r.field): Curve(tuple(r.curve["x"]), tuple(r.curve["y"]))
            for r in rows}  # fmt: skip


@lru_cache(maxsize=8)
def _curves(url: str, version: int) -> dict[Key, Curve]:
    from cdi_adapter.db import session_scope

    with session_scope() as c:
        rows = c.execute(
            text("SELECT s.doc_type, s.fact_type, s.field, s.curve FROM calibration_slice s "
                 "JOIN calibration_model m ON m.id = s.model_id "
                 "WHERE m.version = :v AND s.status = 'fitted'"), {"v": version},
        ).all()  # fmt: skip
        known = c.execute(text("SELECT method FROM calibration_model WHERE version = :v"),
                          {"v": version}).first()  # fmt: skip
    if known is None:
        raise LookupError(f"CDI_CALIBRATOR_VERSION={version}: no such fitted calibrator")
    if known.method != METHOD:
        raise ValueError("legacy calibrator must be refitted with isotonic_pav_v2")
    return {(r.doc_type, r.fact_type, r.field): Curve(tuple(r.curve["x"]), tuple(r.curve["y"]))
            for r in rows}  # fmt: skip


def clear_cache() -> None:
    _curves.cache_clear()


def gate_score(version: int | None, url: str, doc_type: str | None, fact_type: str,
               score: float) -> tuple[float, dict[str, Any] | None]:  # fmt: skip
    """The confidence the gate decides on, and the calibration used (None: the fixed score)."""
    if version is None:
        return score, None
    curves = _curves(url, version)
    doc = doc_type or "unknown"
    used = {k[2]: c.predict(score) for k, c in curves.items()
            if k[0] == doc and k[1] == fact_type}  # fmt: skip
    if not used:
        return score, {"version": version, "slice": None, "reason": "no fitted slice"}
    value = min(used.values())  # a field-level fact: its weakest field
    return value, {"version": version, "raw": score, "fields": used}


# ------------------------------------------------------------------ reliability diagrams


def _svg(title: str, rel: dict[str, Any], curve: dict[str, Any] | None) -> str:
    w = h = 220
    pad = 28

    def px(v: float) -> float:
        return pad + v * (w - 2 * pad)

    def py(v: float) -> float:
        return h - pad - v * (h - 2 * pad)

    parts = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img">',
             f"<title>{html.escape(title)}</title>",
             f'<rect x="{pad}" y="{pad}" width="{w - 2 * pad}" height="{h - 2 * pad}" '
             'fill="none" stroke="#999"/>',
             f'<line x1="{px(0)}" y1="{py(0)}" x2="{px(1)}" y2="{py(1)}" stroke="#bbb" '
             'stroke-dasharray="4 3"/>']  # fmt: skip
    for tag, color in (("before", "#c2410c"), ("after", "#1d4ed8")):
        for b in (rel.get(tag) or {}).get("bins", []):
            r = 2 + min(6, b["n"] ** 0.5)
            parts.append(f'<circle cx="{px(b["mean_predicted"]):.1f}" '
                         f'cy="{py(b["observed"]):.1f}" r="{r:.1f}" fill="{color}" '
                         'fill-opacity=".55"/>')  # fmt: skip
    if curve:
        pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in zip(curve["x"], curve["y"],
                                                                  strict=True))  # fmt: skip
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#1d4ed8"/>')
    parts.append(f'<text x="{pad}" y="16" font-size="11">{html.escape(title)}</text></svg>')
    return "".join(parts)


def write_report(conn: Connection, version: int, path: Path) -> Path:
    """An HTML page: one reliability diagram per slice (orange: before, blue: after)."""
    model = conn.execute(text("SELECT * FROM calibration_model WHERE version = :v"),
                         {"v": version}).mappings().one()  # fmt: skip
    rows = conn.execute(text("SELECT * FROM calibration_slice WHERE model_id = :m "
                             "ORDER BY doc_type, fact_type, field"),
                        {"m": str(model["id"])}).mappings().all()  # fmt: skip
    cards = []
    for s in rows:
        title = f"{s['doc_type']} / {s['fact_type']} / {s['field']}"
        if s["status"] != "fitted":
            body = f"<p>insufficient data ({s['n_train']} training examples)</p>"
        else:
            verdict = {True: "within", False: "OUTSIDE", None: "too few held-out to judge"}[
                s["within_tolerance"]
            ]
            body = (
                _svg(title, s["reliability"], s["curve"])
                + f"<p>ECE {s['ece_before']} → {s['ece_after']} ({verdict} tolerance "
                f"{model['tolerance']}); train {s['n_train']}, held out {s['n_test']}</p>"
            )
        cards.append(f"<section><h3>{html.escape(title)}</h3>{body}</section>")
    page = (f"<!doctype html><meta charset=utf-8><title>Calibrator v{version}</title>"
            f"<h1>Calibrator v{version} (training data v{model['dataset_version']})</h1>"
            f"<p>Tolerance {model['tolerance']}: {html.escape(model['tolerance_basis'])}</p>"
            + "".join(cards))  # fmt: skip
    path.write_text(page, encoding="utf-8")
    return path
