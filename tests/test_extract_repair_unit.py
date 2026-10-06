"""The hf gateway shows the model the schema's keys; the repair keeps every value the model read
(2026-09-25 finding: a correct 7B answer under its own key names was dropped to
``{"confidence": 0.5}`` and held as ``_partial`` with 0 facts)."""

from __future__ import annotations

import copy
from typing import Any

from cdi_adapter.extract.facts import facts_from_payload
from cdi_adapter.ml import client as mc
from cdi_adapter.ml.client import bundled_schema, repair_payload, validate_schema
from cdi_adapter.mlserve.backends import HFBackend, schema_outline, with_schema

RX = "prescription.v3"

# the real 7B answer to a synthetic scanned prescription (Colab, 2026-09-25), before this fix
WRAPPED = {"prescription": {
    "patient": {"name": "Ravi Gupta", "age": "27 Y", "sex": "M", "id": "LT865001"},
    "date": "04/08/2026",
    "diagnosis": "Acid peptic disease, Type 2 diabetes mellitus",
    "medications": [
        {"drug": "Tab Atorvastatin", "strength": "10 mg", "dose_pattern": "0-0-1",
         "duration": "30 days", "route": "oral", "evidence": ["b6"], "confidence": 1},
        {"drug": "Tab Metformin", "strength": "500 mg", "dose_pattern": "1-0-1",
         "duration": "30 days", "route": "oral", "evidence": ["b7"], "confidence": 1},
        {"drug": "Cap Omeprazole", "strength": "20 mg", "dose_pattern": "OD",
         "duration": "14 days", "route": "oral", "evidence": ["b8"], "confidence": 1},
    ],
    "blood_pressure": "145/84 mmHg", "weight": "61 kg", "review_date": "after 1 month",
}}  # fmt: skip

# the shape of the 7B answer to an uploaded screenshot: one medicine, flat at the top level
FLAT = {"medication": "Tab Pantoprazole", "strength": "40 mg", "dose": "1",
        "dose_slot_pattern": "1-0-0", "route": "oral", "duration": "5 days",
        "evidence": ["b30", "b31"], "confidence": 0.9}  # fmt: skip


def test_a_wrapped_answer_with_its_own_key_names_keeps_every_value() -> None:
    r = repair_payload(copy.deepcopy(WRAPPED), RX)
    assert validate_schema(r, RX) == []
    assert [m["drug_name"] for m in r["medications"]] == ["Tab Atorvastatin", "Tab Metformin",
                                                          "Cap Omeprazole"]  # fmt: skip
    m = r["medications"][1]
    assert (m["strength"], m["frequency"], m["duration"], m["route"]) == \
        ("500 mg", "1-0-1", "30 days", "oral")  # fmt: skip
    assert r["diagnoses"] == ["Acid peptic disease, Type 2 diabetes mellitus"]
    assert r["encounter_date"] == "04/08/2026"
    assert r["patient"] == {"name": "Ravi Gupta", "age_text": "27 Y", "sex": "M",
                            "mrn": "LT865001"}  # fmt: skip
    assert {(v["name"], v["value"]) for v in r["vitals"]} == {("blood_pressure", "145/84 mmHg"),
                                                             ("weight", "61 kg")}  # fmt: skip
    assert r["advice"] == [{"text": "Review after 1 month"}]


def test_the_repaired_answer_becomes_facts_for_every_line() -> None:
    facts = facts_from_payload("prescription", repair_payload(copy.deepcopy(WRAPPED), RX))
    kinds = sorted(f.fact_type for f in facts)
    assert kinds == ["advice", "condition", "medication", "medication", "medication",
                     "vital_sign", "vital_sign"]  # fmt: skip
    bp = next(f for f in facts if f.local_text == "blood pressure")
    assert bp.value_text == "145/84"
    wt = next(f for f in facts if f.local_text == "body weight")
    assert wt.value_num == 61


def test_a_lone_flat_medicine_becomes_the_medication_list() -> None:
    r = repair_payload(copy.deepcopy(FLAT), RX)
    assert validate_schema(r, RX) == []
    assert r["medications"] == [{"drug_name": "Tab Pantoprazole", "strength": "40 mg",
                                 "dose": "1", "frequency": "1-0-0", "route": "oral",
                                 "duration": "5 days", "evidence": ["b30", "b31"],
                                 "confidence": 0.9}]  # fmt: skip
    assert r["confidence"] == 0.9


def test_a_schema_shaped_answer_is_left_as_it_is() -> None:
    good = {"medications": [{"drug_name": "Tab X", "strength": "5 mg", "frequency": "OD",
                             "confidence": 0.9}],
            "vitals": [{"name": "Pulse", "value": "76", "confidence": 0.9}],
            "diagnoses": ["Hypertension"], "confidence": 0.9}  # fmt: skip
    assert repair_payload(copy.deepcopy(good), RX) == good


def test_a_name_alone_is_not_taken_for_a_medicine() -> None:
    r = repair_payload({"medication": "Tab X", "evidence": ["b1"], "confidence": 0.8}, RX)
    assert r["medications"] == [{"drug_name": "Tab X", "confidence": 0.5}]  # the old path


def test_a_given_field_is_never_overwritten_by_an_alias() -> None:
    r = repair_payload({"medications": [{"drug_name": "Tab X", "frequency": "BD",
                                         "dose_pattern": "1-0-1", "confidence": 1}],
                        "confidence": 1}, RX)  # fmt: skip
    assert r["medications"][0]["frequency"] == "BD"
    assert r["medications"][0]["dose_pattern"] == "1-0-1"  # kept, not lost


def test_vitals_given_as_an_object_become_vital_items() -> None:
    r = repair_payload({"medications": [], "vitals": {"bp": "120/80", "pulse": "72/min"},
                        "confidence": 1}, RX)  # fmt: skip
    assert r["vitals"] == [
        {"name": "bp", "value": "120/80", "confidence": 0.5},
        {"name": "pulse", "value": "72/min", "confidence": 0.5},
    ]


def test_loose_patient_details_go_into_the_patient_block(monkeypatch) -> None:
    """Colab T4, 2026-09-26: the 7B (int8) wrote an OPD note's demographics at the top level; the
    repair dropped ``name``, ``sex``, ``age_text`` and the upload stayed an "Unnamed patient"."""
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(mc.log, "warning", lambda event, **kw: seen.append((event, kw)))
    r = repair_payload({"name": "Test Patient", "sex": "F", "age_text": "48 Y",
                        "confidence": 0.8}, "opd_note.v3")  # fmt: skip
    assert r["patient"] == {"name": "Test Patient", "sex": "F", "age_text": "48 Y"}
    assert validate_schema(r, "opd_note.v3") == [] and seen == []
    # what the block already has is kept; a loose alias only fills a gap
    r = repair_payload({"patient": {"name": "In Block"}, "patient_name": "Loose", "gender": "M",
                        "age": "30", "confidence": 0.8}, RX)  # fmt: skip
    assert r["patient"] == {"name": "In Block", "sex": "M", "age_text": "30"}


def test_a_key_that_cannot_be_placed_is_logged_by_name_only(monkeypatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(mc.log, "warning", lambda event, **kw: seen.append((event, kw)))
    repair_payload({"medications": [], "hospital_stamp": "PHI-VALUE", "confidence": 1}, RX)
    assert seen == [("repair_dropped_keys", {"schema": RX, "keys": ["hospital_stamp"]})]


# ------------------------------------------------------------------ the prompt


def test_the_outline_has_the_exact_keys_and_no_code_fields() -> None:
    out = schema_outline(bundled_schema(RX))
    for key in ('"medications"', '"drug_name"', '"frequency"', '"duration"', '"route"',
                '"diagnoses"', '"vitals"', '"encounter_date"', '"confidence"'):  # fmt: skip
        assert key in out, key
    assert '"code"' not in out and '"system"' not in out  # codes are S5's job, never the model's


def test_the_hf_backend_shows_the_schema_and_the_stub_contract_is_unchanged() -> None:
    prompts: list[str] = []

    def runner(model, processor, prompt, image, max_tokens):  # noqa: ANN001, ANN202
        prompts.append(prompt)
        return "{}"

    hf = HFBackend(loader=lambda mid, s: (object(), object()), runner=runner)
    hf.generate("Extract this.", None, json_schema=bundled_schema(RX))
    assert prompts[0].startswith("Extract this.")
    assert "Always include: medications, confidence." in prompts[0]
    assert '"drug_name"' in prompts[0]
    hf.generate("Transcribe.", None, json_schema=None)  # OCR transcription: no schema, no change
    assert prompts[1] == "Transcribe."
    assert with_schema("P", None) == "P"


def test_a_boolean_or_number_written_as_text_is_read_as_the_schema_type():
    # 2026-09-25 (live 7B on Colab): three uploads classified "other" and extracted nothing,
    # because the model answered "is_handwritten": "true" and the classification failed the schema
    raw = {"doc_type": "opd_note", "specialty": "cardiology", "is_handwritten": "true",
           "languages": ["te"], "confidence": "0.9",
           "page_spans": [{"doc_type": "opd_note", "start_page": "1",
                           "end_page": "1"}]}  # fmt: skip
    out = repair_payload(copy.deepcopy(raw), "classification.v1")
    assert validate_schema(out, "classification.v1") == []
    assert out["is_handwritten"] is True and out["doc_type"] == "opd_note"
    assert out["page_spans"][0]["start_page"] == 1 and out["page_spans"][0]["end_page"] == 1
    no = repair_payload({"doc_type": "prescription", "is_handwritten": "No", "confidence": 0.8},
                        "classification.v1")  # fmt: skip
    assert no["is_handwritten"] is False
    # only a plain written boolean / number: anything else stays as written (and fails the schema)
    odd = repair_payload({"doc_type": "prescription", "is_handwritten": "partly",
                          "confidence": 0.8}, "classification.v1")  # fmt: skip
    assert odd["is_handwritten"] == "partly" and validate_schema(odd, "classification.v1")
    # a field that accepts text keeps the text: "specialty" stays "true" if that is what was written
    assert mc._scalar("true", {"string", "null"}) == "true"
