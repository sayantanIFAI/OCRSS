"""E4-S2 (unit): isotonic fit, per-field correctness, reliability diagrams."""

from __future__ import annotations

import random

import pytest

from cdi_adapter.training.calibrator import (
    field_outcomes,
    gate_score,
    held_out,
    reliability,
)
from cdi_adapter.training.isotonic import fit


def test_pool_adjacent_violators_on_a_known_case():
    c = fit([0.1, 0.2, 0.3, 0.4], [0, 1, 0, 1])
    assert c.x == (0.1, 0.2, 0.3, 0.4) and c.y == (0.0, 0.5, 0.5, 1.0)
    assert c.predict(0.0) == 0.0 and c.predict(1.0) == 1.0  # clipped outside
    assert c.predict(0.25) == pytest.approx(0.5)  # flat across the pooled block


def test_the_fit_is_monotone_and_recovers_the_true_rate():
    rnd = random.Random(7)
    xs = [rnd.random() for _ in range(4000)]
    ys = [int(rnd.random() < x**2) for x in xs]  # the scores over-state correctness
    c = fit(xs, ys)
    assert all(a <= b for a, b in zip(c.y, c.y[1:], strict=False))
    assert all(a < b for a, b in zip(c.x, c.x[1:], strict=False))
    for v in (0.2, 0.5, 0.8):
        assert c.predict(v) == pytest.approx(v**2, abs=0.08)


def test_equal_scores_are_one_block():
    c = fit([0.5, 0.5, 0.5, 0.9], [1, 0, 0, 1])
    assert c.x == (0.5, 0.9) and c.y[0] == pytest.approx(1 / 3, abs=1e-6)


def _ex(decision, proposed, corrected=None, fact_type="medication"):
    return {"fact_type": fact_type, "decision": decision, "proposed": proposed,
            "corrected": corrected, "proposed_fact_id": "f"}  # fmt: skip


MED = {"local_text": "Tab Amlodipine 5 mg", "drug_text": "Tab Amlodipine 5 mg",
       "code_system": "http://snomed.info/sct", "code": "386864001", "strength_num": 5.0,
       "strength_unit": "mg", "dose_num": 1.0, "frequency_code": "OD",
       "duration_days": 30.0}  # fmt: skip


def test_medication_correctness_is_per_field():
    assert dict(field_outcomes(_ex("confirmed", MED, MED))) == {
        "medicine": 1,
        "strength": 1,
        "dose": 1,
        "frequency": 1,
        "duration": 1,
    }
    fixed = {**MED, "strength_num": 10.0}
    assert dict(field_outcomes(_ex("corrected", MED, fixed))) == {
        "medicine": 1,
        "strength": 0,
        "dose": 1,
        "frequency": 1,
        "duration": 1,
    }
    other = {**MED, "code": "373444002", "local_text": "Tab Atorvastatin 10 mg"}
    assert dict(field_outcomes(_ex("corrected", MED, other)))["medicine"] == 0
    assert set(dict(field_outcomes(_ex("rejected", MED))).values()) == {0}  # all wrong
    no_duration = {**MED, "duration_days": None}
    assert "duration" not in dict(field_outcomes(_ex("confirmed", no_duration, no_duration)))
    # the machine missed a field the reviewer wrote: that field was wrong
    assert dict(field_outcomes(_ex("corrected", no_duration, MED)))["duration"] == 0


def test_lab_orders_diagnoses_and_other_facts():
    lab = {"local_text": "S. Creatinine", "code_system": "http://loinc.org", "code": "2160-0"}
    assert field_outcomes(_ex("confirmed", lab, lab, "lab_order")) == [("lab_test", 1)]
    dx = {"local_text": "HTN", "code": None}
    assert field_outcomes(_ex("corrected", dx, {"local_text": "Hypertension", "code": None},
                              "condition")) == [("diagnosis", 0)]  # fmt: skip
    bp = {"local_text": "BP", "value_text": "138/86"}
    assert field_outcomes(_ex("corrected", bp, {**bp, "value_text": "130/86"},
                              "vital_sign")) == [("value", 0)]  # fmt: skip


def test_reliability_bins_and_calibration_error():
    perfect = reliability([0.05] * 20 + [0.95] * 20, [0] * 19 + [1] + [1] * 19 + [0])
    assert perfect["ece"] == pytest.approx(0.0, abs=1e-9)
    over = reliability([0.9] * 10, [1] * 5 + [0] * 5)
    assert over["ece"] == pytest.approx(0.4) and over["bins"][0]["observed"] == 0.5


def test_the_held_out_split_is_stable_and_about_one_in_five():
    ids = [f"fact-{i}" for i in range(5000)]
    share = sum(held_out(i, "strength") for i in ids) / len(ids)
    assert 0.17 < share < 0.23
    assert [held_out(i, "dose") for i in ids[:50]] == [held_out(i, "dose") for i in ids[:50]]


def test_without_a_calibrator_the_gate_score_is_unchanged():
    assert gate_score(None, "url", "prescription", "medication", 0.97) == (0.97, None)
