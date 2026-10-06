"""E0-S9 unit: seed map shape, normalisation, exact/alias/fuzzy/token binding, bind_fact."""

from __future__ import annotations

import re

import pytest

from cdi_adapter.terminology import seed
from cdi_adapter.terminology.service import (
    CONF_BOUND,
    CONF_LOCAL,
    bind_fact,
    bind_text,
    normalize,
)


def test_seed_size_matches_architecture():
    sizes = {k: len(v) for k, v in {"conditions": seed.CONDITIONS, "symptoms": seed.SYMPTOMS,
             "procedures": seed.PROCEDURES, "labs": seed.LABS, "vitals": seed.VITALS,
             "drugs": seed.DRUGS, "allergens": seed.ALLERGENS}.items()}  # fmt: skip
    assert sizes["conditions"] >= 35 and sizes["symptoms"] >= 10 and sizes["procedures"] >= 4
    assert sizes["labs"] >= 30 and sizes["vitals"] >= 14 and sizes["drugs"] >= 15
    assert sizes["allergens"] >= 6
    assert 110 <= len(seed.ALL_CONCEPTS) <= 140  # "~120-concept" map


def test_seed_code_formats():
    for c in seed.ALL_CONCEPTS:
        if c.system == seed.SNOMED:
            assert re.fullmatch(r"\d{6,18}", c.code), c
        else:
            assert c.system == seed.LOINC and re.fullmatch(r"\d{1,7}-\d", c.code), c
        if c.icd10:
            assert re.fullmatch(r"[A-Z]\d{2}(\.\d{1,2})?", c.icd10), c
    for code, display in seed.ROUTE_SCT.values():
        assert re.fullmatch(r"\d{6,18}", code) and display.endswith("route")


def test_worked_example_codes_are_verbatim_from_architecture():
    """ARCHITECTURE §8.2 lists these bindings."""
    expected = {
        ("condition", "Type 2 diabetes mellitus"): "44054006",
        ("condition", "hypertension"): "59621000",
        ("medication", "Metformin"): "372567009",
        ("medication", "Amlodipine"): "386864001",
        ("medication", "Atorvastatin"): "373444002",
        ("vital_sign", "systolic blood pressure"): "8480-6",
        ("vital_sign", "diastolic blood pressure"): "8462-4",
        ("vital_sign", "body weight"): "29463-7",
        ("lab_result", "HbA1c"): "4548-4",
        ("lab_result", "Fasting Blood Glucose"): "1558-6",
        ("lab_result", "Serum Creatinine"): "2160-0",
        ("lab_result", "Total Cholesterol"): "2093-3",
        ("lab_result", "LDL Cholesterol"): "2089-1",
        ("allergy", "Penicillin"): "373270004",
    }
    for (fact_type, text), code in expected.items():
        b = bind_text(fact_type, text)
        assert b is not None and b.concept.code == code, (fact_type, text)


@pytest.mark.parametrize(
    "raw,norm",
    [("HbA1c", "hba1c"), ("HbAlc", "hba1c"), ("Hb A1c", "hba1c"), ("HB-A1C", "hba1c"),
     ("Serum  Creatinine.", "serum creatinine"), ("S/O  (fever)", "s/o fever")],
)  # fmt: skip
def test_normalize(raw, norm):
    assert normalize(raw) == norm


@pytest.mark.parametrize(
    "fact_type,text,code,how",
    [
        ("condition", "Type 2 Diabetes Mellitus", "44054006", "exact"),
        ("condition", "T2DM", "44054006", "alias"),
        ("condition", "HTN", "59621000", "alias"),
        ("condition", "Hypertensoin", "59621000", "fuzzy"),  # OCR transposition
        ("lab_result", "HbAlc", "4548-4", "alias"),  # Appendix D OCR reading
        ("medication", "Tab Ecosprin 75 mg", "387458008", "token"),  # brand -> aspirin
        ("medication", "TAB METPURE XL 50 MG", "372826007", "token"),
        ("medication", "Metformin", "372567009", "exact"),
        ("symptom", "breathlessness", "267036007", "alias"),
        ("procedure", "PTCA to LAD", "415070008", "alias"),
        ("allergy", "Aspirin", "387458008", "exact"),
    ],
)
def test_bind_text(fact_type, text, code, how):
    b = bind_text(fact_type, text)
    assert (b.concept.code, b.how) == (code, how)


@pytest.mark.parametrize(
    "fact_type,text",
    [("condition", "some rare syndrome"), ("medication", "Tab Unknownium 5 mg"),
     ("finding", "LAD 90% stenosis"), ("advice", "low salt diet"), ("condition", ""),
     ("condition", "Diabetic nephropathy")],  # close to a seed term but a different disease
)  # fmt: skip
def test_unknown_terms_are_never_force_coded(fact_type, text):
    assert bind_text(fact_type, text) is None


def test_bind_fact_bound_and_local_only():
    u = bind_fact({"fact_type": "lab_result", "local_text": "HbA1c", "value_unit_ucum": "%"})
    assert (u["code_system"], u["code"], u["code_status"], u["confidence_terminology"]) == (
        seed.LOINC, "4548-4", "bound", CONF_BOUND)  # fmt: skip
    u = bind_fact({"fact_type": "condition", "local_text": "rare thing", "value_unit_ucum": None})
    assert (u["code"], u["code_status"], u["confidence_terminology"]) == (None, "local_only",
                                                                           CONF_LOCAL)  # fmt: skip


def test_bind_fact_medication_and_units():
    u = bind_fact({"fact_type": "medication", "local_text": "Tab Amlong 5 mg",
                   "value_unit_ucum": None, "frequency_per_day": None,
                   "frequency_code": "BD", "dose_unit_ucum": "mg"})  # fmt: skip
    assert u["medication"] == {"rxlike_system": seed.SNOMED, "rxlike_code": "386864001",
                               "frequency_per_day": 2, "dose_unit_ucum": "mg"}  # fmt: skip
    u = bind_fact({"fact_type": "lab_result", "local_text": "Glucose",
                   "value_unit_ucum": "mg/dl"})  # fmt: skip
    assert u["value_unit_ucum"] == "mg/dL"
