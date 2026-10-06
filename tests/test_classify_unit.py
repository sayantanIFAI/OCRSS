"""E0-S6 unit: the scored heuristic classifier and the VLM fallback prompt."""

from __future__ import annotations

import pytest

from cdi_adapter.classify import service as cs
from cdi_adapter.classify.prompt import DOC_TYPES, build_classification_prompt

CARDIO_RX = """CARDIAC CATH LABORATORY - CareFlow Heart Institute
Dr. R. Menon MD DM (Cardiology)  Reg No 12345
Name: Ramesh Kumar  Age/Sex: 55/M  Date: 12/08/2026
Diagnosis: CAD - post PTCA to LAD
Rx
Tab Ecosprin 75 mg 0-1-0 after food x 30 days
Tab Clopidogrel 75 mg 0-1-0 after food
Tab Atorvastatin 40 mg 0-0-1
Tab Metoprolol XL 25 mg 1-0-0
Review after 1 month with lipid profile"""

LAB = """CAREFLOW DIAGNOSTICS - LABORATORY REPORT
Patient: Anjali Das  Age: 48 Y  Sample collected: 11/08/2026  Specimen: Blood
Test                    Result    Unit     Biological Reference Range
HbA1c                   7.8       %        4.0 - 5.6
Fasting Blood Glucose   142       mg/dL    70 - 100
Serum Creatinine        1.1       mg/dL    0.6 - 1.3
Total Cholesterol       212       mg/dL    < 200
Pathologist: Dr. S. Gupta"""

ANGIO = """DEPARTMENT OF CARDIOLOGY - CORONARY ANGIOGRAPHY REPORT
Name: Ramesh Kumar   Age: 55   Date of study: 10/08/2026
Procedure: Coronary angiogram via right radial approach
Findings: LAD proximal 90% stenosis. LCX mild plaque. RCA normal.
Impression: Single vessel coronary artery disease involving LAD.
Advice: PTCA to LAD. Radiologist / Interventional cardiologist: Dr R Menon"""

DISCHARGE = """DISCHARGE SUMMARY - CareFlow Hospital
Name: Priya Nair  Age: 58  Date of Admission: 01/08/2026  Date of Discharge: 06/08/2026
Final Diagnosis: Community acquired pneumonia
Course in hospital: treated with IV antibiotics, improved.
Condition at discharge: stable. Advice on discharge: complete oral antibiotics, review in 1 week"""


@pytest.mark.parametrize(
    "text,expected",
    [(CARDIO_RX, "prescription"), (LAB, "lab_report"), (ANGIO, "radiology"),
     (DISCHARGE, "discharge_summary")],
)  # fmt: skip
def test_heuristic_classifies_clean_printed_pages(text, expected):
    res = cs._heuristic_classify(text)
    assert res is not None and res.doc_type == expected
    assert res.score >= cs.MIN_SCORE and res.score - res.runner_up_score >= cs.MIN_LEAD


def test_cardiac_cath_laboratory_is_not_a_lab_report():
    scores = cs.score_text(CARDIO_RX)
    assert scores["prescription"] >= 9  # ARCHITECTURE §5 S2: "scores 9+ for prescription"
    assert scores["lab_report"] <= 1  # "~0 for lab_report"


def test_short_page_falls_through_to_vlm():
    assert cs._heuristic_classify("Rx Tab Ecosprin 75 mg 0-1-0") is None  # < 180 chars


def test_garbled_page_falls_through_to_vlm():
    garbled = ("Rx ~~ ##!! @@ Tab ** 0-1-0 ;; .. ,, // \\\\ ^^ %% $$ &&& ***" * 6)[:400]
    assert len(garbled) >= 180
    assert cs._heuristic_classify(garbled) is None  # < 75 % alphanumeric: likely handwriting


def test_weak_or_tied_signal_falls_through_to_vlm():
    weak = ("General note from the clinic about the patient visit today. " * 5).strip()
    assert cs._heuristic_classify(weak) is None
    tie = LAB + "\n" + DISCHARGE  # two documents scanned together: 12 vs 12
    scores = cs.score_text(tie)
    assert scores["lab_report"] == scores["discharge_summary"]
    assert cs._heuristic_classify(tie) is None  # lead < 3 -> never guess, ask the VLM


def test_word_boundaries_prevent_substring_hits():
    assert cs.score_text("Laboratory tabulated drxw examination-free")["prescription"] == 0


def test_dose_strength_signal_ignores_lab_units():
    assert cs.score_text("Tab X 75 mg")["prescription"] == 3  # tab 2 + strength 1
    assert cs.score_text("Glucose 142 mg/dL")["prescription"] == 0


def test_every_signal_type_is_a_valid_classification_doc_type():
    assert set(cs._SIGNALS) <= set(DOC_TYPES) and len(cs._SIGNALS) == 8
    assert sum(len(v) for v in cs._SIGNALS.values()) >= 35  # "~35 weighted patterns"
    assert {w for v in cs._SIGNALS.values() for _, w in v} == {1, 2, 3}


def test_prompt_lists_11_types_and_wraps_hint():
    p = build_classification_prompt("hello world")
    assert len(DOC_TYPES) == 11 and all(t in p for t in DOC_TYPES)
    assert "<document>\nhello world\n</document>" in p
    assert len(build_classification_prompt("x" * 10_000)) < 6_000  # hint is capped
