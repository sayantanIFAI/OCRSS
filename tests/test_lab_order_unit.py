"""Tests a prescription advises (owner request 2026-09-28), pure parts: reading them into the
schema, one fact per test, their codes, and how the record carries them."""

from __future__ import annotations

import json
import re

import pytest

from cdi_adapter.extract.facts import HANDLERS, split_orders
from cdi_adapter.extract.prompt import GUIDANCE, build_extraction_prompt
from cdi_adapter.fhir.service import advised_test
from cdi_adapter.ml.client import repair_payload, validate_schema
from cdi_adapter.mlserve.backends import StubBackend
from cdi_adapter.terminology import seed
from cdi_adapter.terminology.service import bind_text, order_key


@pytest.mark.parametrize(("written", "tests"), [
    ("CBC, LFT, RFT", ["CBC", "LFT", "RFT"]),
    ("T3 + T4 + TSH", ["T3", "T4", "TSH"]),
    ("Lipid profile and HbA1c", ["Lipid profile", "HbA1c"]),
    ("S. Creatinine; Hb", ["S. Creatinine", "Hb"]),
    ("Urine R/M", ["Urine R/M"]),  # a slash is part of the test's name
    ("Urine C/S", ["Urine C/S"]),
    ("Random blood sugar", ["Random blood sugar"]),  # "and" only as a word
    ("", []),
])  # fmt: skip
def test_one_advised_test_per_item(written, tests):
    assert split_orders(written) == tests


@pytest.mark.parametrize(("written", "code"), [
    ("CBC", "58410-2"), ("Hemogram", "58410-2"), ("LFT", "24325-3"), ("KFT", "24362-6"),
    ("RFT", "24362-6"), ("Lipid profile", "57698-3"), ("Urine R/M", "24356-8"),
    ("S. Creatinine", "2160-0"), ("Sr. TSH", "3016-3"), ("HbA1c", "4548-4"), ("FBS", "1558-6"),
    ("Vitamin D level", "1989-3"), ("Blood group", "882-1"), ("X-ray chest", "399208008"),
    ("2D Echo", "40701008"), ("ECG", "29303009"), ("USG whole abdomen", "45036003"),
])  # fmt: skip
def test_advised_tests_are_coded(written, code):
    b = bind_text("lab_order", written)
    assert b is not None and b.concept.code == code, (written, b)


def test_an_unknown_test_keeps_its_text_uncoded():
    assert bind_text("lab_order", "TFT") is None  # no verified thyroid-panel code: never forced
    assert bind_text("lab_order", "Dengue NS1") is None
    # an advised panel never binds a lab RESULT: results keep their own table
    assert bind_text("lab_result", "CBC") is None


def test_the_words_around_a_test_are_not_the_test():
    assert order_key("s creatinine") == "creatinine"
    assert order_key("vitamin d level") == "vitamin d"
    assert order_key("lipid profile test") == "lipid profile"
    assert order_key("sr tsh") == "tsh"


def test_order_codes_are_well_formed():
    for c in seed.ORDER_CONCEPTS:
        if c.system == seed.SNOMED:
            assert re.fullmatch(r"\d{6,18}", c.code), c
        else:
            assert c.system == seed.LOINC and re.fullmatch(r"\d{1,7}-\d", c.code), c
    assert not set(seed.ORDER_CONCEPTS) & set(seed.ALL_CONCEPTS)  # the ~120-concept map is as was


def test_the_prompt_asks_for_advised_tests_on_prescriptions_and_opd_notes():
    for doc_type in ("prescription", "opd_note"):
        assert "investigations_advised" in GUIDANCE[doc_type]
        assert "Never list a test the page does not name" in GUIDANCE[doc_type]
    assert "investigations_advised" not in GUIDANCE["lab_report"]


@pytest.mark.parametrize("schema", ["prescription.v3", "opd_note.v3"])
def test_the_schemas_take_advised_tests(schema):
    payload = {"investigations_advised": [{"text": "CBC", "confidence": 0.9}], "confidence": 0.9}
    if schema == "prescription.v3":
        payload["medications"] = []
    assert validate_schema(payload, schema) == []


def test_a_model_writing_investigations_is_repaired_to_advised_tests():
    out = repair_payload({"medications": [], "investigations": ["CBC", "LFT"],
                          "confidence": 0.9}, "prescription.v3")  # fmt: skip
    assert "investigations" not in out
    assert [i["text"] if isinstance(i, dict) else i for i in out["investigations_advised"]] == [
        "CBC", "LFT"]  # fmt: skip
    # a lab report's tests are results and keep their own key
    lab = repair_payload({"tests": [{"name": "Hb", "value": 12}], "confidence": 0.9},
                         "lab_report.v3")  # fmt: skip
    assert "investigations_advised" not in lab and lab["tests"]


@pytest.mark.parametrize("doc_type", ["prescription", "opd_note"])
def test_advised_tests_become_lab_order_facts(doc_type):
    payload = {"investigations_advised": [{"text": "CBC, S. Creatinine", "confidence": 0.9,
                                           "evidence": ["b7"]}, "Urine R/M"],
               "medications": [], "confidence": 0.9}  # fmt: skip
    facts = [f for f in HANDLERS[doc_type](payload, 0.8) if f.fact_type == "lab_order"]
    assert [f.local_text for f in facts] == ["CBC", "S. Creatinine", "Urine R/M"]
    assert facts[0].evidence == facts[1].evidence  # both from the same written line


def test_the_stub_reads_an_investigations_line():
    blocks = [{"id": f"id{i}", "text": t} for i, t in enumerate([
        "Name: Test Person  Age: 40 Y", "Rx", "Tab Metformin 500 mg 1-0-1",
        "Investigations: CBC, LFT", "Adv: plenty of fluids"])]  # fmt: skip
    prompt, _ = build_extraction_prompt("prescription", blocks)
    out = json.loads(StubBackend().generate(prompt, json_schema={"$id": "cdi:prescription.v3"}))
    assert [i["text"] for i in out["investigations_advised"]] == ["CBC", "LFT"]
    assert [i["text"] for i in out["advice"]] == ["plenty of fluids"]  # advice stays advice
    assert validate_schema(out, "prescription.v3") == []


def test_the_record_carries_an_advised_test_as_written_with_its_standard_name():
    assert advised_test("KFT", "Renal function panel - Serum or Plasma") == (
        "Test advised: KFT (Renal function panel - Serum or Plasma)")  # fmt: skip
    assert advised_test("TFT", None) == "Test advised: TFT"
    assert advised_test("ECG", "ecg") == "Test advised: ECG"  # nothing more to say


def test_the_reviewer_shows_a_tests_advised_section():
    from cdi_adapter.webapp.reviewer_page import SCRIPT

    assert "['lab_order', 'Tests advised']" in SCRIPT
