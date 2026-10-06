"""Gate thresholds derived per class from calibration data (backlog E4-S3, §15.10).

``derive_thresholds`` turns an E4-S2 calibrator into the S6 gate's auto-accept cut-offs:

* a **class** is doc_type x fact_type x field (the calibrator's slices) x **evidence state**;
* for each class, on the calibrator's training examples (calibrated), the cut-off is the lowest
  score at which the **false-auto-accept rate** - wrong values among those accepted - stays within
  the target, judged by its one-sided Wilson upper bound (so a lucky small sample never passes)
  and with at least ``min_accepted`` examples accepted; it is then **measured again on the held-out
  examples** the calibrator never saw;
* **numeric fields** (strength, dose, duration) use the stricter numeric target; there is no
  universal constant - every derived value comes from the data;
* evidence states: ``single_engine`` is what the pipeline produces today and is derived;
  ``engines_disagree`` is always review (a rule, §15.6); ``engines_agree``,
  ``known_doctor_strong_exemplar`` and ``unknown_doctor`` are ``not_reachable`` until a second
  independent reader (E2-S11) and doctor identification (E6-S11, E15) exist - never guessed;
* the targets are placeholders until the clinical safety owner approves the false-auto-accept
  requirement (the AC's documented NFR); they are stored with that basis.

The set is recorded in ``gate_threshold_set`` / ``gate_threshold`` and written as a JSON config
file to check in; the gate reads the file named by ``CDI_GATE_THRESHOLDS`` (unset: the global
constants). ``gate_decision`` is the gate's use of it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from cdi_adapter import repo
from cdi_adapter.training import calibration, calibrator

CONFIG_SCHEMA = "gate_thresholds.v1"
VALIDATION_METHOD = "held_out_wilson_v2"
DEFAULT_TARGET = 0.02
DEFAULT_TARGET_NUMERIC = 0.005
TARGET_BASIS = "placeholder: the clinical safety owner approves the false-auto-accept NFR (E4-S3)"
DEFAULT_MIN_ACCEPTED = 20
Z = 1.645  # one-sided 95%
BOUND = f"wilson-upper(z={Z});held-out-v2"
MIN_HELD_OUT_FOR_VERDICT = 10
NUMERIC_FIELDS = ("strength", "dose", "duration")
DERIVED_STATE = "single_engine"
STATES: dict[str, tuple[str, str]] = {
    "single_engine": ("derived", "one recognizer read it: what the pipeline produces today"),
    "engines_agree": ("not_reachable", "needs a second independent reader (E2-S11)"),
    "engines_disagree": ("always_review", "rule: independent readings disagree (§15.6)"),
    "known_doctor_strong_exemplar": ("not_reachable", "needs doctor identification (E6-S11, E15)"),
    "unknown_doctor": ("not_reachable", "needs doctor identification (E6-S11, E15)"),
}


def wilson_upper(errors: int, n: int, z: float = Z) -> float:
    if n == 0:
        return 1.0
    p = errors / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return min(1.0, centre + half)


def derive_cutoff(points: list[tuple[float, int]], target: float, min_accepted: int,
                  z: float = Z) -> tuple[float | None, dict[str, Any]]:  # fmt: skip
    """The lowest calibrated score whose accepted set (score >= it) meets the target."""
    if not math.isfinite(target) or not 0 < target < 1 or min_accepted < 1:
        raise ValueError("target must be in (0,1) and min_accepted positive")
    if any(not math.isfinite(x) or not 0 <= x <= 1 or y not in (0, 1) for x, y in points):
        raise ValueError("threshold examples require finite scores and binary outcomes")
    pts = sorted(points)
    n = len(pts)
    suffix_err = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_err[i] = suffix_err[i + 1] + (1 - pts[i][1])
    i = 0
    while i < n:
        t = pts[i][0]
        accepted, errors = n - i, suffix_err[i]
        if accepted < min_accepted:
            break
        if wilson_upper(errors, accepted, z) <= target:
            return t, {"n_accepted": accepted, "false_accept": round(errors / accepted, 5)}
        while i < n and pts[i][0] == t:  # the next distinct score
            i += 1
    return None, {"n_accepted": 0, "false_accept": None}


def derive_thresholds(conn: Connection, calibrator_version: int, *,
                      target: float = DEFAULT_TARGET,
                      target_numeric: float = DEFAULT_TARGET_NUMERIC,
                      min_accepted: int = DEFAULT_MIN_ACCEPTED,
                      actor: str = "threshold-derive") -> dict[str, Any]:  # fmt: skip
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('threshold_derive'))"))
    model = conn.execute(text("SELECT dataset_version, method FROM calibration_model "
                              "WHERE version = :v"),
                         {"v": calibrator_version}).first()  # fmt: skip
    if model is None:
        raise LookupError(f"no fitted calibrator v{calibrator_version} (E4-S2 fit first)")
    if model.method != calibrator.METHOD:
        raise ValueError("legacy calibrator must be refitted before deriving thresholds")
    if not 0 < target_numeric <= target < 1:
        raise ValueError("targets must satisfy 0 < numeric target <= default target < 1")
    curves = calibrator.curves_for(conn, calibrator_version)
    examples = calibrator.slice_examples(calibration.load(conn, model.dataset_version))
    rows: list[dict[str, Any]] = []
    for key, curve in sorted(curves.items()):
        doc_type, fact_type, fld = key
        goal = target_numeric if fld in NUMERIC_FIELDS else target
        ex = examples.get(key, [])
        derive = [(curve.predict(x), y) for x, y, held in ex if not held]
        held_out = [(curve.predict(x), y) for x, y, held in ex if held]
        for state, (kind, why) in STATES.items():
            r: dict[str, Any] = {"doc_type": doc_type, "fact_type": fact_type, "field": fld,
                                 "evidence_state": state, "target": goal, "threshold": None,
                                 "n_derive": 0, "n_accepted_derive": 0,
                                 "false_accept_derive": None, "n_accepted_held_out": 0,
                                 "false_accept_held_out": None, "meets_target": None,
                                 "status": kind, "basis": why}  # fmt: skip
            if state == DERIVED_STATE:
                t, got = derive_cutoff(derive, goal, min_accepted)
                r["n_derive"] = len(derive)
                if t is None:
                    r["status"] = "insufficient_data"
                    r["basis"] = (f"no cut-off keeps false auto-accepts <= {goal} "
                                  f"({BOUND}) with >= {min_accepted} accepted")  # fmt: skip
                else:
                    acc = [y for p, y in held_out if p >= t]
                    fa = round(1 - sum(acc) / len(acc), 5) if acc else None
                    r |= {"threshold": t, "n_accepted_derive": got["n_accepted"],
                          "false_accept_derive": got["false_accept"],
                          "n_accepted_held_out": len(acc), "false_accept_held_out": fa,
                          "meets_target": (wilson_upper(len(acc) - sum(acc), len(acc)) <= goal
                                           if fa is not None
                                           and len(acc) >= MIN_HELD_OUT_FOR_VERDICT else None),
                          "basis": f"derived on calibrator v{calibrator_version} data"}  # fmt: skip
            rows.append(r)
    version = (conn.execute(text("SELECT max(version) FROM gate_threshold_set")).scalar_one()
               or 0) + 1  # fmt: skip
    derived = [r for r in rows if r["status"] == "derived"]
    short = sum(r["status"] == "insufficient_data" for r in rows)
    summary = {"classes": len(rows), "derived": len(derived),
               "meets_target": sum(bool(r["meets_target"]) for r in derived),
               "insufficient_data": short}  # fmt: skip
    set_id = conn.execute(
        text("INSERT INTO gate_threshold_set (version, calibrator_version, target_default, "
             "  target_numeric, target_basis, bound, min_accepted, summary, created_by) "
             "VALUES (:v, :c, :t, :tn, :tb, :b, :m, CAST(:s AS jsonb), :by) RETURNING id"),
        {"v": version, "c": calibrator_version, "t": target, "tn": target_numeric,
         "tb": TARGET_BASIS, "b": BOUND, "m": min_accepted, "s": json.dumps(summary),
         "by": actor},
    ).scalar_one()  # fmt: skip
    for r in rows:
        conn.execute(
            text("INSERT INTO gate_threshold (set_id, doc_type, fact_type, field, "
                 "  evidence_state, status, threshold, target, n_derive, n_accepted_derive, "
                 "  false_accept_derive, n_accepted_held_out, false_accept_held_out, "
                 "  meets_target, basis) "
                 "VALUES (:s, :doc_type, :fact_type, :field, :evidence_state, :status, "
                 "  :threshold, :target, :n_derive, :n_accepted_derive, :false_accept_derive, "
                 "  :n_accepted_held_out, :false_accept_held_out, :meets_target, :basis)"),
            {**r, "s": str(set_id)},
        )  # fmt: skip
    repo.write_audit(conn, actor=actor, action="create", entity="gate_threshold_set",
                     entity_id=str(set_id),
                     detail={"version": version, "calibrator_version": calibrator_version,
                             **summary})  # fmt: skip
    return {"version": version, "calibrator_version": calibrator_version, **summary}


def config_for(conn: Connection, version: int) -> dict[str, Any]:
    """The checked-in form of a derived set: what the gate needs, nothing else."""
    s = conn.execute(text("SELECT * FROM gate_threshold_set WHERE version = :v"),
                     {"v": version}).mappings().one()  # fmt: skip
    if s["bound"] != BOUND:
        raise ValueError("legacy threshold set must be rederived with held-out Wilson validation")
    rows = conn.execute(
        text("SELECT doc_type, fact_type, field, evidence_state, status, threshold, target, "
             "  false_accept_held_out, meets_target FROM gate_threshold WHERE set_id = :s "
             "ORDER BY doc_type, fact_type, field, evidence_state"), {"s": str(s["id"])},
    ).mappings().all()  # fmt: skip
    return {"schema": CONFIG_SCHEMA, "validation_method": VALIDATION_METHOD, "version": version,
            "calibrator_version": s["calibrator_version"],
            "targets": {"default": s["target_default"], "numeric": s["target_numeric"]},
            "target_basis": s["target_basis"], "bound": s["bound"],
            "thresholds": [dict(r) for r in rows]}  # fmt: skip


def write_config(conn: Connection, version: int, path: Path) -> Path:
    path.write_text(json.dumps(config_for(conn, version), indent=1, default=str) + "\n",
                    encoding="utf-8")  # fmt: skip
    return path


# ------------------------------------------------------------------ the gate


@dataclass(frozen=True)
class Thresholds:
    version: int
    calibrator_version: int
    classes: dict[tuple[str, str, str, str], tuple[str, float | None]] = field(default_factory=dict)


@lru_cache(maxsize=4)
def load_config(path: str) -> Thresholds:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"{path}: not a {CONFIG_SCHEMA} file")
    if raw.get("validation_method") != VALIDATION_METHOD:
        raise ValueError(
            "threshold configuration must be regenerated with held-out Wilson validation"
        )
    classes = {}
    for t in raw["thresholds"]:
        if t["status"] == "derived" and t["threshold"] is None:
            raise ValueError(f"{path}: a derived class without a threshold")
        key = (t["doc_type"], t["fact_type"], t["field"], t["evidence_state"])
        if key in classes:
            raise ValueError(f"{path}: duplicate threshold class")
        cutoff = t["threshold"]
        if cutoff is not None and (
            isinstance(cutoff, bool)
            or not isinstance(cutoff, int | float)
            or not math.isfinite(cutoff)
            or not 0 <= cutoff <= 1
        ):
            raise ValueError(f"{path}: threshold must be a finite probability")
        status = t["status"]
        if status == "derived" and t.get("meets_target") is not True:
            status = "unvalidated"
        if t["evidence_state"] == "engines_disagree":
            status = "always_review"
        classes[key] = (status, cutoff)
    return Thresholds(int(raw["version"]), int(raw["calibrator_version"]), classes)


def clear_cache() -> None:
    load_config.cache_clear()


def check_config(path: str | None, calibrator_version: int | None) -> Thresholds | None:
    """The gate's thresholds, or None; refuses thresholds on another calibrator's scale."""
    if not path:
        return None
    cfg = load_config(path)
    if calibrator_version != cfg.calibrator_version:
        raise ValueError(
            f"CDI_GATE_THRESHOLDS ({path}) is on the scale of calibrator "
            f"v{cfg.calibrator_version}, but CDI_CALIBRATOR_VERSION is {calibrator_version}"
        )
    return cfg


def evidence_state(matrix: list[dict[str, Any]]) -> str:
    """From the E4-S8 evidence matrix: how many independent readers, and do they agree."""
    readers = [s for s in matrix if s.get("tier") == 1 and s.get("relation") == "supports"]
    if len({s["candidate"] for s in readers}) > 1 or any(
        s.get("tier") == 1 and s.get("relation") == "contradicts" for s in matrix
    ):
        return "engines_disagree"
    return "engines_agree" if len({s["source"] for s in readers}) > 1 else "single_engine"


def gate_decision(cfg: Thresholds, doc_type: str | None, fact_type: str,
                  field_scores: dict[str, float], state: str, held_by_rules: bool
                  ) -> tuple[str | None, dict[str, Any] | None]:  # fmt: skip
    """``auto_accepted`` only when every field clears its class's threshold; ``in_review``
    otherwise. A configured policy never falls back to global constants for a missing class."""
    doc = doc_type or "unknown"
    fields: dict[str, Any] = {}
    expected = {k[2] for k in cfg.classes if k[0] == doc and k[1] == fact_type}
    ok = not held_by_rules and state != "engines_disagree" and expected <= field_scores.keys()
    for fld, score in sorted(field_scores.items()):
        status, t = cfg.classes.get((doc, fact_type, fld, state), ("no_class", None))
        passed = status == "derived" and t is not None and score >= t
        fields[fld] = {"score": score, "threshold": t, "status": status, "passed": passed}
        ok = ok and passed
    info = {"version": cfg.version, "evidence_state": state, "fields": fields,
            "held_by_rules": held_by_rules}  # fmt: skip
    return ("auto_accepted" if ok and fields else "in_review"), info
