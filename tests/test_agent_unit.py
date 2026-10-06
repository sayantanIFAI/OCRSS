"""The medication agent's pure parts (backlog E12-S4): diagnoses, the prompts, the guard."""

from __future__ import annotations

import json

from cdi_adapter.agent.medication import (
    Line,
    choose_prompt,
    diagnoses_of,
    fit_prompt,
    guard,
    match_diagnosis,
    verify_prompt,
)
from cdi_adapter.ml.client import validate_schema
from cdi_adapter.mlserve.backends import StubBackend, with_schema


def _line(**kw) -> Line:
    base = {"n": 1, "fact_id": "f1", "read_as": "Muronorm", "strengths": (62.5,), "form": None,
            "catalogue": None}  # fmt: skip
    return Line(**(base | kw))


THYRONORM = {"id": 17, "name": "Thyronorm 62.5mcg Tablet", "brand": "thyronorm",
             "manufacturer": "Test Pharma M", "composition": "Thyroxine (62.5mcg)",
             "strengths": [62.5]}  # fmt: skip
MERONORM = {"id": 18, "name": "Meronorm 1gm Injection", "brand": "meronorm",
            "manufacturer": "Test Pharma N", "composition": "Meropenem (1000mg)",
            "strengths": [1000.0]}  # fmt: skip


def test_the_prescriptions_diagnoses_one_per_item():
    facts = [
        {
            "fact_type": "condition",
            "local_text": "Rheumatoid Arthritis | Hypothyroid | GC Induced OP",
        },
        {"fact_type": "condition", "local_text": "CPAP | Bronchial Asthma"},
        {"fact_type": "symptom", "local_text": "Fever, cough"},
        {"fact_type": "medication", "local_text": "Tab Saaz 500"},
        {"fact_type": "condition", "local_text": "hypothyroid", "is_current": True},  # a repeat
    ]
    assert diagnoses_of(facts) == ["Rheumatoid Arthritis", "Hypothyroid", "GC Induced OP", "CPAP",
                                   "Bronchial Asthma", "Fever", "cough"]  # fmt: skip


def test_treats_must_be_one_of_the_prescriptions_diagnoses():
    dx = ["Rheumatoid Arthritis", "Hypothyroid", "GC Induced OP"]
    assert match_diagnosis("Hypothyroid", dx) == "Hypothyroid"
    assert match_diagnosis("hypothyroidism", dx) == "Hypothyroid"
    assert match_diagnosis("rheumatoid arthritis", dx) == "Rheumatoid Arthritis"
    assert match_diagnosis("Hypertension", dx) is None  # the model may not add a diagnosis
    assert match_diagnosis("null", dx) is None  # the 7B writes the string "null"
    assert match_diagnosis(None, dx) is None and match_diagnosis("x", []) is None


def test_the_prompts_carry_the_reading_the_options_and_the_diagnoses():
    ln = _line(options=[THYRONORM, MERONORM])
    c = choose_prompt([ln])
    assert '[1] first read as "Muronorm", strength 62.5' in c
    assert "1) Thyronorm 62.5mcg Tablet - Thyroxine (62.5mcg)" in c
    assert "2) Meronorm 1gm Injection" in c and "choice 0" in c
    f = fit_prompt(["Hypothyroid"], [(1, "Thyronorm 62.5mcg Tablet", "Thyroxine (62.5mcg)")])
    assert "- Hypothyroid" in f and "[1] Thyronorm 62.5mcg Tablet - contains Thyroxine" in f
    assert "[1] Thyroxine (62.5mcg) for Hypothyroid" in verify_prompt(
        [("Thyroxine (62.5mcg)", "Hypothyroid")]
    )


def test_agent_questions_are_not_bound_by_the_copy_from_the_page_rule():
    """The extraction preamble forbids values not written on the page; the agent's fit questions
    ask for medical knowledge, so they get their own preamble (measured 2026-09-26: with the
    extraction one the 7B answered "null" for every line)."""
    agent = with_schema("q", {"$id": "cdi:med_agent_fit.v1", "properties": {"lines": {}}})
    extraction = with_schema("q", {"$id": "cdi:prescription.v3", "properties": {"x": {}}})
    assert "Never add a value" in extraction and "Never add a value" not in agent
    assert 'not the string "null"' in agent


def test_the_guard_accepts_a_choice_that_looks_like_the_reading():
    ln = _line(options=[THYRONORM, MERONORM])
    v = guard(ln, {"line": 1, "choice": 1, "evidence": "Thyronorm", "rationale": "as written",
                   "confidence": 0.9}, 0.45)  # fmt: skip
    assert v["status"] == "suggested" and v["product"]["name"] == "Thyronorm 62.5mcg Tablet"
    assert 0.5 < v["confidence"] <= 0.9


def test_the_guard_ignores_evidence_that_only_copies_the_option_and_vetoes():
    """ADR-0003 rule 4: the model quoted the option's own name as its "evidence"; nothing the
    page gave looks like the product, so it is vetoed, never suggested."""
    ln = _line(read_as="Shaz", options=[THYRONORM])
    v = guard(ln, {"line": 1, "choice": 1, "evidence": "Thyronorm", "rationale": "thyroid",
                   "confidence": 0.95}, 0.45)  # fmt: skip
    assert v["status"] == "vetoed" and v["confidence"] < 0.45


def test_a_suggestion_always_carries_its_evidence_text_and_a_rationale():
    """E12-S4 AC: suggested value + the exact evidence text it is grounded in + a one-sentence
    rationale, even when the model left evidence or rationale empty."""
    ln = _line(read_as="Thyronorm", options=[THYRONORM])
    v = guard(ln, {"line": 1, "choice": 1, "evidence": None, "rationale": None,
                   "confidence": 0.9}, 0.45)  # fmt: skip
    assert v["status"] == "suggested" and v["product"]["name"] == "Thyronorm 62.5mcg Tablet"
    assert v["evidence"] == "Thyronorm"  # the line's own reading: what the grounding measured
    assert v["rationale"].startswith('"Thyronorm" as written on the page matches')
    # an independent quote that grounds better is the evidence; the model's rationale is kept
    ln = _line(read_as="Thyr", options=[THYRONORM])
    v = guard(ln, {"line": 1, "choice": 1, "evidence": "Thyronorn", "rationale": "as written",
                   "confidence": 0.9}, 0.45)  # fmt: skip
    assert v["evidence"] == "Thyronorn" and v["rationale"] == "as written"


def test_the_guard_drops_none_and_out_of_range_choices():
    ln = _line(options=[THYRONORM])
    assert guard(ln, {"line": 1, "choice": 0, "evidence": None, "rationale": None,
                      "confidence": 0.8}, 0.45)["status"] == "none"  # fmt: skip
    assert guard(ln, {"line": 1, "choice": 7, "evidence": "x", "rationale": None,
                      "confidence": 0.8}, 0.45)["status"] == "none"  # fmt: skip
    assert guard(ln, None, 0.45)["status"] == "none"


def test_the_stub_answers_the_agent_schemas_validly():
    stub = StubBackend()
    for name in ("med_agent_choice.v1", "med_agent_fit.v1", "med_agent_verify.v1"):
        schema = {"$id": f"cdi:{name}"}
        assert validate_schema(json.loads(stub.generate("x", json_schema=schema)), name) == []
