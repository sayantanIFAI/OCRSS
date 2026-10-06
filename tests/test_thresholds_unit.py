"""E4-S3 (unit): deriving a per-class cut-off, the evidence state, the gate rule."""

from __future__ import annotations

import json

import pytest

from cdi_adapter.training import thresholds as th


def test_the_wilson_upper_bound_is_conservative():
    assert th.wilson_upper(0, 100) == pytest.approx(0.0264, abs=5e-4)
    assert th.wilson_upper(0, 10) > th.wilson_upper(0, 100)  # fewer examples: less certain
    assert th.wilson_upper(5, 100) > 0.05  # an observed 5% is not proof of <= 5%
    assert th.wilson_upper(0, 0) == 1.0


def _points(n: int = 1000, cut: float = 0.8) -> list[tuple[float, int]]:
    return [(i / n, int(i / n >= cut)) for i in range(n)]  # right only at or above `cut`


def test_the_cutoff_is_the_lowest_score_that_meets_the_target():
    t, got = th.derive_cutoff(_points(), target=0.02, min_accepted=20)
    assert t == pytest.approx(0.8, abs=1e-9)  # exactly where the errors stop
    assert got["false_accept"] == 0.0 and got["n_accepted"] == 200


def test_a_noisy_class_needs_a_higher_cutoff_and_numeric_targets_are_stricter():
    noisy = [(i / 1000, int(i % 50 != 0)) for i in range(1000)]  # 2% wrong everywhere
    strict, _ = th.derive_cutoff(noisy, target=0.005, min_accepted=20)
    loose, _ = th.derive_cutoff(noisy, target=0.05, min_accepted=20)
    assert strict is None  # 2% wrong at every score: a 0.5% target is never met
    assert loose is not None


def test_too_few_accepted_examples_is_no_cutoff():
    assert th.derive_cutoff(_points(n=30), target=0.02, min_accepted=20)[0] is None


def test_the_evidence_state_comes_from_the_matrix():
    one = [{"tier": 1, "relation": "supports", "source": "rapidocr", "candidate": "X"}]
    assert th.evidence_state(one) == "single_engine"
    two = one + [{"tier": 1, "relation": "supports", "source": "trocr", "candidate": "X"}]
    assert th.evidence_state(two) == "engines_agree"
    split = one + [{"tier": 1, "relation": "supports", "source": "trocr", "candidate": "Y"}]
    assert th.evidence_state(split) == "engines_disagree"
    against = one + [{"tier": 1, "relation": "contradicts", "source": "trocr", "candidate": "X"}]
    assert th.evidence_state(against) == "engines_disagree"


CFG = th.Thresholds(1, 1, {
    ("prescription", "medication", "medicine", "single_engine"): ("derived", 0.9),
    ("prescription", "medication", "strength", "single_engine"): ("derived", 0.97),
    ("prescription", "medication", "medicine", "engines_disagree"): ("always_review", None),
    ("prescription", "medication", "strength", "engines_disagree"): ("always_review", None),
})  # fmt: skip


def test_a_fact_auto_accepts_only_when_every_field_clears_its_threshold():
    ok, info = th.gate_decision(
        CFG,
        "prescription",
        "medication",
        {"medicine": 0.95, "strength": 0.98},
        "single_engine",
        False,
    )
    assert ok == "auto_accepted" and info["fields"]["strength"]["passed"]
    low, info = th.gate_decision(
        CFG,
        "prescription",
        "medication",
        {"medicine": 0.95, "strength": 0.96},
        "single_engine",
        False,
    )
    assert low == "in_review" and not info["fields"]["strength"]["passed"]
    ruled, _ = th.gate_decision(
        CFG,
        "prescription",
        "medication",
        {"medicine": 0.99, "strength": 0.99},
        "single_engine",
        True,
    )
    assert ruled == "in_review"  # an S6 blocker / warn / partial still holds it
    split, _ = th.gate_decision(
        CFG,
        "prescription",
        "medication",
        {"medicine": 0.99, "strength": 0.99},
        "engines_disagree",
        False,
    )
    assert split == "in_review"
    unknown, _ = th.gate_decision(
        CFG, "prescription", "medication", {"medicine": 0.99, "dose": 0.99}, "single_engine", False
    )
    assert unknown == "in_review"  # a field with no class is never waved through
    assert (
        th.gate_decision(CFG, "lab_report", "lab_result", {"value": 0.99}, "single_engine", False)[
            0
        ]
        == "in_review"
    )  # configured policy has no validated class


def test_the_config_file_is_validated(tmp_path):
    good = {"schema": th.CONFIG_SCHEMA, "validation_method": th.VALIDATION_METHOD,
            "version": 3, "calibrator_version": 2,
            "thresholds": [{"doc_type": "prescription", "fact_type": "medication",
                            "field": "medicine", "evidence_state": "single_engine",
                            "status": "derived", "threshold": 0.9,
                            "meets_target": True}]}  # fmt: skip
    path = tmp_path / "t.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    th.clear_cache()
    cfg = th.check_config(str(path), 2)
    assert cfg is not None and cfg.version == 3
    with pytest.raises(ValueError, match="scale of calibrator v2"):
        th.check_config(str(path), None)  # thresholds without their calibrator
    assert th.check_config(None, None) is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**good, "schema": "other"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a gate_thresholds.v1"):
        th.load_config(str(bad))
    th.clear_cache()
