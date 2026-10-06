"""Adversarial Epic 4 calibration and cascade regression checks."""

import copy
import json

import pytest

from cdi_adapter.resolution.cascade import LevelResult, Signal, parse_policy, resolve
from cdi_adapter.training import calibrator, thresholds
from cdi_adapter.training.isotonic import fit
from tests.test_resolution_unit import RAW


def test_pooled_isotonic_values_remain_flat_at_training_points():
    curve = fit([0.1, 0.2, 0.3, 0.4], [0, 1, 0, 1])
    assert [curve.predict(x) for x in (0.1, 0.2, 0.3, 0.4)] == [0, 0.5, 0.5, 1]
    assert curve.predict(0.25) == 0.5


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1])
def test_calibration_refuses_invalid_scores(bad):
    with pytest.raises(ValueError):
        fit([bad], [1])
    with pytest.raises(ValueError):
        thresholds.derive_cutoff([(bad, 1)], 0.01, 20)


def test_dose_unit_corrections_are_errors():
    before = {"dose_num": 1, "dose_unit_ucum": "mg"}
    after = {"dose_num": 1, "dose_unit_ucum": "g"}
    assert (
        dict(
            calibrator.field_outcomes(
                {
                    "fact_type": "medication",
                    "proposed": before,
                    "corrected": after,
                    "decision": "corrected",
                }
            )
        )["dose"]
        == 0
    )


def test_training_and_held_out_do_not_share_a_document():
    rows = [
        {
            "document_id": "same-document",
            "proposed_fact_id": str(i),
            "doc_type": "lab_report",
            "fact_type": "lab_result",
            "confidence": 0.9,
            "proposed": {"value_num": i},
            "corrected": {"value_num": i},
            "decision": "confirmed",
        }
        for i in range(50)
    ]
    points = calibrator.slice_examples(rows)[("lab_report", "lab_result", "value")]
    assert len({held for _, _, held in points}) == 1


@pytest.mark.parametrize("verdict", [False, None])
def test_unvalidated_thresholds_cannot_auto_accept(tmp_path, verdict):
    path = tmp_path / "threshold.json"
    path.write_text(
        json.dumps(
            {
                "schema": thresholds.CONFIG_SCHEMA,
                "validation_method": thresholds.VALIDATION_METHOD,
                "version": 1,
                "calibrator_version": 1,
                "thresholds": [
                    {
                        "doc_type": "lab_report",
                        "fact_type": "lab_result",
                        "field": "value",
                        "evidence_state": "single_engine",
                        "status": "derived",
                        "threshold": 0.8,
                        "meets_target": verdict,
                    }
                ],
            }
        )
    )
    cfg = thresholds.load_config(str(path))
    assert (
        thresholds.gate_decision(
            cfg, "lab_report", "lab_result", {"value": 1.0}, "single_engine", False
        )[0]
        == "in_review"
    )


def test_disagreement_cannot_be_enabled_by_a_threshold_config():
    cfg = thresholds.Thresholds(
        1, 1, {("prescription", "medication", "medicine", "engines_disagree"): ("derived", 0.1)}
    )
    assert (
        thresholds.gate_decision(
            cfg, "prescription", "medication", {"medicine": 1.0}, "engines_disagree", False
        )[0]
        == "in_review"
    )


def test_missing_scored_fields_cannot_auto_accept():
    cfg = thresholds.Thresholds(
        1,
        1,
        {
            ("prescription", "medication", "medicine", "single_engine"): ("derived", 0.8),
            ("prescription", "medication", "strength", "single_engine"): ("derived", 0.9),
        },
    )
    assert (
        thresholds.gate_decision(
            cfg, "prescription", "medication", {"medicine": 1.0}, "single_engine", False
        )[0]
        == "in_review"
    )


def test_cross_tier_conflict_and_nonleading_contradiction_always_review():
    levels = {"L1": LevelResult("A")}
    for signals in (
        [Signal("pixels", 0, "A", "supports"), Signal("trocr", 1, "B", "supports")],
        [Signal("pixels", 0, "A", "supports"), Signal("trocr", 1, "B", "contradicts")],
    ):
        assert resolve(signals, levels, field_="medication", cohort="printed").outcome == "review"


def test_policy_cannot_disable_conflict_tiers():
    raw = copy.deepcopy(RAW)
    raw["rules"]["conflict_forces_review_tiers"] = []
    with pytest.raises(ValueError, match="conflicts must force review"):
        parse_policy(raw)


def test_gate_does_not_round_a_score_up_across_a_threshold(monkeypatch):
    from cdi_adapter.training.isotonic import Curve

    monkeypatch.setattr(
        calibrator,
        "_curves",
        lambda *args: {("lab_report", "lab_result", "value"): Curve((0.0, 1.0), (0.0, 1.0))},
    )
    score, _ = calibrator.gate_score(1, "unused", "lab_report", "lab_result", 0.89996)
    assert score < 0.9


def test_multiple_readers_are_not_assumed_to_agree(monkeypatch):
    from cdi_adapter.resolution import facts

    monkeypatch.setattr(
        facts,
        "_observations",
        lambda *args: [
            {"engine": "rapidocr", "region_kind": "printed", "ocr_conf": 0.9},
            {"engine": "trocr", "region_kind": "printed", "ocr_conf": 0.9},
        ],
    )
    fact = {"fact_type": "vital_sign", "local_text": "pulse", "value_num": 80}
    assert facts.resolve_fact(None, fact, []).outcome == "review"


def test_assumed_policy_cannot_auto_accept_with_a_real_backend(monkeypatch):
    from cdi_adapter.config import get_settings
    from cdi_adapter.resolution import facts

    monkeypatch.setenv("CDI_MLSERVE_BACKEND", "hf")
    get_settings.cache_clear()
    monkeypatch.setattr(
        facts,
        "_observations",
        lambda *args: [
            {"engine": "rapidocr", "region_kind": "printed", "ocr_conf": 1.0},
        ],
    )
    try:
        decision = facts.resolve_fact(None, {"fact_type": "vital_sign", "local_text": "pulse"}, [])
        assert decision.outcome == "review"
        assert any(step["step"] == "not_validated" for step in decision.trace)
    finally:
        get_settings.cache_clear()


def test_legacy_threshold_validation_is_not_silently_reused(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            {
                "schema": thresholds.CONFIG_SCHEMA,
                "version": 1,
                "calibrator_version": 1,
                "thresholds": [],
            }
        )
    )
    with pytest.raises(ValueError, match="regenerated"):
        thresholds.load_config(str(path))
