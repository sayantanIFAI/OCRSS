"""E0-S8 unit: tolerant readers, fact handlers, medication parsing, prompts, mismatch rule."""

from __future__ import annotations

from datetime import date

import pytest

from cdi_adapter.extract import facts as fx
from cdi_adapter.extract.prompt import (
    SCHEMA_FOR_DOC_TYPE,
    build_extraction_prompt,
    max_tokens_for,
)
from cdi_adapter.extract.service import check_mismatch
from cdi_adapter.mpi.service import Candidate
from cdi_adapter.terminology.parse import duration_days, freq_per_day, is_prn, to_ucum

# ---------------------------------------------------------------- readers


@pytest.mark.parametrize(
    "x,expected",
    [
        ({"value": 150, "unit": "mg"}, (150.0, "mg", None)),
        (150, (150.0, None, None)),
        ("500 mg", (500.0, "mg", None)),
        ("7.8 %", (7.8, "%", None)),
        ("138/86", (None, None, "138/86")),
        ({"value": "72", "unit": "kg"}, (72.0, "kg", None)),
        ("positive", (None, None, "positive")),
        (None, (None, None, None)),
        ({"value": None, "unit": "mg"}, (None, "mg", None)),
    ],
)
def test_qty_accepts_object_number_or_string(x, expected):
    assert fx._qty(x) == expected


@pytest.mark.parametrize(
    "x,expected",
    [({"text": "HTN"}, "HTN"), ({"display": "T2DM"}, "T2DM"), ({"name": "x"}, "x"),
     ({"code": "44054006"}, "44054006"), ("Asthma", "Asthma"), ("  ", None), ({}, None)],
)  # fmt: skip
def test_coded_text(x, expected):
    assert fx._coded_text(x) == expected


# ---------------------------------------------------------------- frequency / duration / units


@pytest.mark.parametrize(
    "text,expected",
    [("1-0-1", 2), ("1-1-1", 3), ("0-0-1", 1), ("1-1-1-1", 4), ("BD", 2), ("TDS", 3),
     ("OD", 1), ("once daily", 1), ("every 8 hours", 3), ("TWICE IN A YEAR", 0.006),
     ("once a week", 0.143), ("x 15 days", None), ("", None)],
)  # fmt: skip
def test_freq_per_day(text, expected):
    got = freq_per_day(text)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_duration_is_not_frequency():
    assert freq_per_day("x 15 days") is None
    assert duration_days("x 15 days") == 15 and duration_days("for 2 weeks") == 14
    assert duration_days("1 month") == 30 and duration_days("daily") is None


def test_prn_and_ucum():
    assert is_prn("SOS for fever") and is_prn("as needed") and not is_prn("sosage")
    assert [to_ucum(u) for u in ("mg", "mcg", "mmHg", "mg/dl", "°F", "IU", "bpm", "xyz")] == [
        "mg", "ug", "mm[Hg]", "mg/dL", "[degF]", "[iU]", "/min", None]  # fmt: skip


# ---------------------------------------------------------------- medication detail (§5 S4)


@pytest.mark.parametrize(
    "item,drug,strength,fpd,days,route",
    [
        ({"drug_name": "TAB METPURE XL 50 MG", "frequency": "1-0-1", "duration": "x 15 days"},
         "Metpure XL", (50.0, "mg"), 2.0, 15.0, None),
        ({"drug_name": "INJ PROLIA 60 MG", "frequency": "TWICE IN A YEAR"},
         "Prolia", (60.0, "mg"), 0.006, None, "injection"),
        ({"drug_name": "INSULIN GLARGINE PEN 100 IU/ML 3 ML", "frequency": "HS"},
         "Insulin Glargine", (100.0, "iu"), 1, None, "SC"),
        ({"drug_name": "Tab Ecosprin 75 mg 0-1-0 after food x 30 days"},
         "Ecosprin", (75.0, "mg"), 1.0, 30.0, None),
        ({"drug_name": "Metformin", "strength": {"value": 500, "unit": "mg"}, "frequency": "BD",
          "route": "oral"}, "Metformin", (500.0, "mg"), 2.0, None, "oral"),
    ],
)  # fmt: skip
def test_parse_medication(item, drug, strength, fpd, days, route):
    m = fx.parse_medication(item)
    assert m.drug_text == drug
    assert (m.strength_num, m.strength_unit) == strength
    assert m.frequency_per_day == pytest.approx(fpd)
    assert m.duration_days == days and m.route == route


def test_parse_medication_prn():
    m = fx.parse_medication({"drug_name": "Tab Dolo 650", "instructions": "SOS for fever"})
    assert m.prn is True and m.form == "tablet" and m.drug_text == "Dolo 650"


# ---------------------------------------------------------------- handlers


def test_vitals_one_bp_fact_dedupe_and_numeric_only():
    p = {"vitals": [
        {"name": "BP", "value": "138/86", "evidence": ["b3"], "confidence": 0.9},
        {"name": "Pulse", "value": 78, "unit": "bpm", "confidence": 0.9},
        {"name": "pulse", "value": 80, "confidence": 0.9},        # duplicate -> dropped
        {"name": "Temperature", "value": "afebrile", "confidence": 0.9},  # no number -> dropped
        {"name": "height", "value": {"value": 170, "unit": "cm"}, "confidence": 0.9},
        {"name": "mystery", "value": 5, "confidence": 0.9},        # unknown vital -> dropped
    ], "confidence": 0.9}  # fmt: skip
    out = fx.facts_from_payload("vitals_sheet", p)
    assert [(f.local_text, f.value_num, f.value_text, f.value_unit_ucum) for f in out] == [
        ("blood pressure", None, "138/86", "mm[Hg]"), ("heart rate", 78.0, None, "/min"),
        ("body height", 170.0, None, "cm")]  # fmt: skip
    assert out[0].evidence == ["b3"]


def test_lab_ranges_flags_and_text_values():
    p = {"tests": [
        {"name": "HbA1c", "value": 7.8, "unit": "%", "ref_range": "4.0-5.6", "flag": "H",
         "confidence": 0.95},
        {"name": "Cholesterol", "value": "212 mg/dL", "ref_range": "< 200", "confidence": 0.9},
        {"name": "Urine sugar", "value": "Nil", "confidence": 0.9},
        {"name": "Empty", "value": None, "confidence": 0.9},
    ], "confidence": 0.9}  # fmt: skip
    a, b, c = fx.facts_from_payload("lab_report", p)
    assert (a.value_num, a.value_unit_ucum, a.ref_range_low, a.ref_range_high,
            a.abnormal_flag) == (7.8, "%", 4.0, 5.6, "H")  # fmt: skip
    assert (b.value_num, b.value_unit_ucum, b.ref_range_low, b.ref_range_text) == (
        212.0, "mg/dL", None, "< 200")  # fmt: skip
    assert (c.value_kind, c.value_text) == ("text", "Nil")


def test_prescription_handler_counts_like_the_worked_example():
    p = {"diagnoses": ["T2DM", {"text": "Hypertension"}],
         "medications": [{"drug_name": d, "confidence": 0.9}
                         for d in ("Tab Metformin 500 mg", "Tab Amlodipine 5 mg",
                                   "Tab Atorvastatin 10 mg")],
         "vitals": [{"name": "BP", "value": "138/86", "confidence": 0.9},
                    {"name": "Weight", "value": "72 kg", "confidence": 0.9}],
         "confidence": 0.9}  # fmt: skip
    out = fx.facts_from_payload("prescription", p)
    kinds = [f.fact_type for f in out]
    assert kinds.count("condition") == 2 and kinds.count("medication") == 3
    assert kinds.count("vital_sign") == 2  # BP (one fact) + weight
    assert all(f.medication for f in out if f.fact_type == "medication")


def test_a_diagnosis_line_naming_two_diseases_becomes_two_conditions():
    p = {"diagnoses": [{"text": "Type 2 diabetes mellitus, Hypertension", "evidence": ["b3"],
                        "confidence": 0.8}]}  # fmt: skip
    out = [f for f in fx.facts_from_payload("prescription", p) if f.fact_type == "condition"]
    assert [f.local_text for f in out] == ["Type 2 diabetes mellitus", "Hypertension"]
    assert all(f.evidence == ["b3"] and f.confidence_extract == 0.8 for f in out)
    # one disease written with a comma, or a part that codes to nothing: left as written
    for one in ("Diabetes mellitus, type 2", "Fracture, left radius", "Heart failure"):
        assert fx.split_joined("condition", one) == [one]
    assert fx.split_joined("condition", "Heart failure and hypertension") == [
        "Heart failure", "hypertension"]  # fmt: skip


def test_radiology_handler_emits_findings_dx_report_procedure():
    p = {"findings_list": ["LAD 90% stenosis", {"text": "LCX mild plaque"}],
         "impression_concepts": ["Single vessel CAD"], "study": "Coronary angiogram",
         "procedure": {"name": "Coronary angiography"}, "advice": ["PTCA to LAD"],
         "confidence": 0.9}  # fmt: skip
    out = fx.facts_from_payload("radiology", p)
    assert [f.fact_type for f in out] == ["finding", "finding", "condition", "diagnostic_report",
                                          "procedure", "advice"]  # fmt: skip
    report = out[3]
    assert report.local_text == "Coronary angiogram" and report.value_text == "Single vessel CAD"


def test_opd_referral_discharge_handlers():
    opd = fx.facts_from_payload("referral", {"chief_complaints": ["fever"], "diagnoses": ["URTI"],
                                             "referral_to": "ENT", "confidence": 0.9})  # fmt: skip
    assert [f.fact_type for f in opd] == ["symptom", "condition", "referral"]
    dc = fx.facts_from_payload("discharge_summary", {
        "diagnoses": ["CAP"], "procedures": ["Bronchoscopy"],
        "investigations": [{"name": "WBC", "value": 14000, "confidence": 0.9}],
        "allergies": [{"substance": "Penicillin", "reaction": "rash"}],
        "confidence": 0.9})  # fmt: skip
    assert [f.fact_type for f in dc] == ["condition", "procedure", "lab_result", "allergy"]
    assert dc[3].value_text == "rash"


def test_item_confidence_falls_back_to_document_confidence():
    out = fx.facts_from_payload("prescription", {"diagnoses": ["T2DM"], "medications": [],
                                                 "confidence": 0.77})  # fmt: skip
    assert out[0].confidence_extract == 0.77


def test_every_classified_type_has_a_schema_and_a_handler():
    assert set(SCHEMA_FOR_DOC_TYPE) == set(fx.HANDLERS)


# ---------------------------------------------------------------- prompt


def test_prompt_block_ids_budget_and_guidance():
    blocks = [{"id": f"uuid-{i}", "text": t} for i, t in enumerate(["Rx", "Tab A 5 mg 1-0-1"])]
    prompt, bmap = build_extraction_prompt("prescription", blocks)
    assert bmap == {"b1": "uuid-0", "b2": "uuid-1"}
    assert "<document>\n[b1] Rx\n[b2] Tab A 5 mg 1-0-1\n</document>" in prompt
    assert "EVERY medication" in prompt and "prescription.v3" in prompt
    assert max_tokens_for("prescription") == 2600 and max_tokens_for("lab_report") == 2000
    assert max_tokens_for("opd_note") == 2400 and max_tokens_for("unknown") == 1600


# ---------------------------------------------------------------- mismatch guard rule


@pytest.mark.parametrize(
    "found,mismatch",
    [
        (Candidate(name="Anjali Das", birth_year=1978), False),
        (Candidate(name="Mrs Anjali Das"), False),
        (Candidate(), False),  # nothing legible -> nothing contradicts
        (Candidate(name="Ramesh Kumar", birth_year=1978), True),
        (Candidate(name="Anjali Das", birth_year=1990), True),
        (Candidate(name="Anjali Das", dob=date(1979, 5, 1)), False),  # +-1 yr
    ],
)
def test_check_mismatch(found, mismatch):
    assert check_mismatch({"name": "Anjali Das", "dob": "1978-01-01"}, found) is mismatch


def test_the_prompt_forbids_filled_in_values_and_keeps_advice_out_of_medications():
    """2026-09-26, handwritten Rx on the 7B: every drug got "1-0-1" and route "oral" (the schema's
    examples), gargles / steam inhalation / fluids came back as medications, and drug names were
    the print OCR's garble."""
    prompt, _ = build_extraction_prompt("prescription", [{"id": "x", "text": "Tab PCM 650 SOS"}])
    assert "never fill in a typical value" in prompt
    assert "never copy an example from the schema as a value" in prompt
    assert "steam inhalation" in prompt and "are advice, not medications" in prompt
    assert "read the words from the image" in prompt  # the OCR text is not the truth


def test_every_document_with_medicines_gets_the_medication_rules():
    """2026-09-28, a cardiology OPD sheet on the 7B: the prescription rules were missing for OPD
    notes, so every drug again got "1-0-1" and route "PO", and drug_name was the form word."""
    for doc_type in ("prescription", "opd_note", "discharge_summary"):
        prompt, _ = build_extraction_prompt(doc_type, [{"id": "x", "text": "Rx"}])
        assert "never fill in a typical value such as 1-0-1 or PO" in prompt
        assert "never the form word alone" in prompt


@pytest.mark.parametrize(
    ("item", "drug", "form", "strength"),
    [
        ({"drug_name": "CAP", "strength": "LOPACA CV 200 MG"}, "Lopaca CV", "capsule", 200.0),
        ({"drug_name": "T", "strength": "DURAI 10"}, "Durai 10", "tablet", None),
        ({"drug_name": "Tab.", "strength": "BUTERO"}, "Butero", "tablet", None),
        # untouched: a real name, or a strength that is only a number and a unit
        ({"drug_name": "Tab Dolo 650", "strength": "650mg"}, "Dolo 650", "tablet", 650.0),
        ({"drug_name": "Sompraz", "strength": "40 mg"}, "Sompraz", None, 40.0),
    ],
)
def test_a_name_written_into_strength_is_put_back(item, drug, form, strength):
    m = fx.parse_medication(fx.reslot_name(item))
    assert (m.drug_text, m.form, m.strength_num) == (drug, form, strength)


def test_the_medication_fact_carries_the_reslotted_name():
    facts = fx._med_facts({"medications": [{"drug_name": "CAP", "strength": "LOPACA CV 200 MG",
                                            "confidence": 0.9}]}, 0.8)  # fmt: skip
    assert facts[0].local_text == "CAP LOPACA CV 200 MG"
    assert facts[0].medication.drug_text == "Lopaca CV"


@pytest.mark.parametrize(
    ("written", "drug", "form"),
    [
        ("T Dytor 10", "Dytor 10", "tablet"), ("C Pan-D", "Pan-D", "capsule"),
        ("Cr Candid", "Candid", "cream"), ("Syr Ascoril LS", "Ascoril LS", "syrup"),
        ("Gtt Moxicip", "Moxicip", "drops"), ("Resp Budecort 0.5", "Budecort 0.5", "respules"),
        ("Vitamin C 500", "Vitamin C 500", None),  # "C" inside a name is not a form
        ("Neurobion Forte T", "Neurobion Forte T", None),
    ],
)  # fmt: skip
def test_the_extracted_form_uses_the_shared_catalogue_table(written, drug, form):
    m = fx.parse_medication({"drug_name": written})
    assert (m.drug_text, m.form) == (drug, form)
