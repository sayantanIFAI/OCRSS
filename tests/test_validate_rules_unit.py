"""E0-S10 unit: S6 rules, calibration, and the governance gate table (ARCHITECTURE §5 S6, §14)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from cdi_adapter.config import Settings
from cdi_adapter.validate import rules as R
from cdi_adapter.validate.rules import Finding
from cdi_adapter.validate.service import _calibrate, audit_sampled, gate

S = Settings(_env_file=None)


def _lab(code, v, unit):
    return {"fact_type": "lab_result", "code": code, "value_num": v, "value_unit_ucum": unit}


def _vital(code, v, unit):
    return {"fact_type": "vital_sign", "code": code, "value_num": v, "value_unit_ucum": unit}


# ---------------------------------------------------------------- value range


@pytest.mark.parametrize(
    "fact,codes",
    [
        (_lab("4548-4", 7.8, "%"), []),
        (_lab("4548-4", 78, "%"), ["range-implausible"]),  # a misread decimal point
        (_lab("1558-6", 142, "mg/dL"), []),
        (_lab("1558-6", 7.9, "mmol/L"), ["unit-mismatch"]),
        (_vital("8480-6", 138, "mm[Hg]"), []),
        (_vital("8480-6", 1380, "mm[Hg]"), ["range-implausible"]),
        (_vital("8867-4", 78, "/min"), []),
        (_vital("59408-5", 101, "%"), ["range-implausible"]),
        ({"fact_type": "lab_result", "code": "9999-9", "value_num": 5}, []),  # no window known
        (_lab("4548-4", None, "%"), []),
    ],
)
def test_check_value_range(fact, codes):
    findings, _ = R.check_value_range(fact)
    assert [f.code for f in findings] == codes
    assert all(f.severity == "blocker" for f in findings if f.code == "range-implausible")


@pytest.mark.parametrize("unit", ["[degF]", None])
def test_temperature_fahrenheit_is_normalised_to_celsius(unit):
    findings, upd = R.check_value_range(_vital("8310-5", 101.3, unit))
    assert upd == {"value_num": 38.5, "value_unit_ucum": "Cel"} and findings == []


def test_unit_present():
    assert [f.code for f in R.check_unit_present(_lab("4548-4", 7.8, None))] == ["unit-missing"]
    assert R.check_unit_present(_lab("4548-4", 7.8, "%")) == []
    assert R.check_unit_present({"fact_type": "condition"}) == []


# ---------------------------------------------------------------- medication


def _med(**kw):
    base = {"fact_type": "medication", "code": "372567009", "strength_num": 500,
            "strength_unit": "mg", "frequency_per_day": 2, "prn": False}  # fmt: skip
    return {**base, **kw}


@pytest.mark.parametrize(
    "fact,codes",
    [
        (_med(), []),
        (_med(strength_num=None), ["med-no-dose"]),
        (_med(frequency_per_day=None), ["med-no-frequency"]),
        (_med(frequency_per_day=None, prn=True), []),  # SOS needs no schedule
        (_med(strength_num=100, frequency_per_day=8), ["med-frequency-implausible"]),
        (_med(frequency_per_day=8), ["med-frequency-implausible", "med-dose-ceiling"]),
        (_med(frequency_per_day=0), ["med-frequency-implausible"]),
        (_med(strength_num=1000, frequency_per_day=3), []),  # 3000 <= 1.5 x 2550
        (_med(strength_num=1500, frequency_per_day=3), ["med-dose-ceiling"]),  # 4500 mg/day
        (_med(code="386864001", strength_num=5, frequency_per_day=1), []),  # amlodipine 5
        (_med(code="386864001", strength_num=50, frequency_per_day=1), ["med-dose-ceiling"]),
        (_med(strength_num=0.5, strength_unit="g", frequency_per_day=2), []),  # 1 g/day
        (_med(code=None, strength_num=99999), []),  # unbound drug: no ceiling known
    ],
)
def test_check_medication(fact, codes):
    assert [f.code for f in R.check_medication(fact)] == codes


def test_prolia_twice_a_year_is_plausible():
    fact = _med(code="unknown", strength_num=60, frequency_per_day=0.006)
    assert R.check_medication(fact) == []


# ---------------------------------------------------------------- dates, evidence, terminology

TODAY = date(2026, 9, 21)


@pytest.mark.parametrize(
    "value,codes",
    [
        (datetime(2026, 8, 12, tzinfo=UTC), []),
        (datetime(2027, 1, 1, tzinfo=UTC), ["date-future"]),
        (datetime(1850, 1, 1, tzinfo=UTC), ["date-implausible"]),
        (datetime(2020, 1, 1, tzinfo=UTC), ["date-far-from-encounter"]),
    ],
)
def test_check_dates(value, codes):
    f = {"effective_time": value}
    assert [x.code for x in R.check_dates(f, date(2026, 8, 12), TODAY)] == codes


def test_asserted_time_is_not_compared_to_encounter():
    f = {"asserted_time": datetime(2026, 9, 21, tzinfo=UTC)}
    assert R.check_dates(f, date(2020, 1, 1), TODAY) == []


def test_check_evidence():
    assert R.check_evidence([{"ocr_block_ids": ["x"], "extracted_text": None}]) == []
    assert R.check_evidence([{"ocr_block_ids": [], "extracted_text": "Tab X"}]) == []
    (f,) = R.check_evidence([{"ocr_block_ids": [], "extracted_text": "  "}])
    assert (f.severity, f.code) == ("blocker", "evidence-missing")
    assert R.check_evidence([])[0].code == "evidence-missing"


def test_check_terminology():
    assert (
        R.check_terminology({"fact_type": "condition", "code_status": "local_only"})[0].code
        == "unmapped-terminology"
    )
    assert R.check_terminology({"fact_type": "condition", "code_status": "bound"}) == []
    assert R.check_terminology({"fact_type": "finding", "code_status": "local_only"}) == []


# ---------------------------------------------------------------- calibration + gate


@pytest.mark.parametrize(
    "conf,partial,warn,block,expected",
    [(0.99, False, 0, 0, 0.99), (0.99, True, 0, 0, 0.69), (0.99, False, 1, 0, 0.9),
     (0.99, False, 0, 1, 0.5), (0.2, True, 0, 0, 0.0), (0.7, False, 1, 1, 0.5)],
)  # fmt: skip
def test_calibrate(conf, partial, warn, block, expected):
    assert _calibrate(conf, partial, warn, block, 0.30) == pytest.approx(expected)


W = Finding("warn", "unit-missing", "")
B = Finding("blocker", "range-implausible", "")
M = Finding("warn", "med-no-frequency", "")


@pytest.mark.parametrize(
    "fact_type,conf,findings,partial,state",
    [
        ("lab_result", 0.99, [], False, "auto_accepted"),  # >= 0.985 & no findings
        ("lab_result", 0.96, [], False, "auto_accepted"),  # >= 0.95 & no blockers (audited)
        ("lab_result", 0.96, [W], False, "auto_accepted"),
        ("lab_result", 0.99, [B], False, "in_review"),  # any blocker
        ("lab_result", 0.99, [], True, "in_review"),  # _partial is never auto-trusted
        ("lab_result", 0.84, [], False, "in_review"),  # below the review floor
        ("lab_result", 0.90, [], False, "in_review"),  # between floor and audit band
        ("medication", 0.99, [M], False, "in_review"),  # med-* finding holds a medication
        ("condition", 0.96, [M], False, "auto_accepted"),  # med-* rule only for medications
    ],
)
def test_gate_table(fact_type, conf, findings, partial, state):
    assert gate(fact_type, conf, findings, partial, S) == state


def test_audit_sampling_draws_at_the_configured_rate():
    import random

    random.seed(7)
    picked = sum(audit_sampled(0.05) for _ in range(20_000))
    assert 0.04 < picked / 20_000 < 0.06
    assert not any(audit_sampled(0.0) for _ in range(1_000))
    assert all(audit_sampled(1.0) for _ in range(1_000))


@pytest.mark.parametrize(
    "bp,codes",
    [("138/86", []), ("380/86", ["range-implausible"]), ("138/200", ["range-implausible"]),
     ("30/10", ["range-implausible", "range-implausible"]), ("high", ["range-implausible"])],
)  # fmt: skip
def test_blood_pressure_is_checked_per_component(bp, codes):
    fact = {"fact_type": "vital_sign", "code": R.BP_PANEL, "value_text": bp, "value_num": None}
    findings, _ = R.check_value_range(fact)
    assert [f.code for f in findings] == codes
