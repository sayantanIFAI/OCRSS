"""The medication agent's recall step (owner request 2026-09-28), pure parts: the line as
extracted, the recall prompt (extracted text + diagnoses), cleaning the model's answer, which
shortlisted products the recall pointed to, and the choose prompt with and without recall."""

from __future__ import annotations

import json

from cdi_adapter.agent.medication import (
    Line,
    _names,
    choose_prompt,
    from_recall,
    line_text,
    recall_prompt,
)
from cdi_adapter.ml.client import validate_schema
from cdi_adapter.mlserve.backends import StubBackend, with_schema

THYRONORM = {"id": 17, "name": "Thyronorm 62.5mcg Tablet", "brand": "thyronorm",
             "composition": "Thyroxine (62.5mcg)", "strengths": [62.5]}  # fmt: skip
ELTROXIN = {"id": 30, "name": "Eltroxin 62.5mcg Tablet", "brand": "eltroxin",
            "composition": "Thyroxine (62.5mcg)", "strengths": [62.5]}  # fmt: skip
MERONORM = {"id": 18, "name": "Meronorm 1gm Injection", "brand": "meronorm",
            "composition": "Meropenem (1000mg)", "strengths": [1000.0]}  # fmt: skip


def _line(**kw) -> Line:
    base = {"n": 1, "fact_id": "f1", "read_as": "Muronorm", "strengths": (62.5,), "form": None,
            "catalogue": None}  # fmt: skip
    return Line(**(base | kw))


def test_the_line_as_extracted_carries_every_detail_read():
    f = {"local_text": "Tab Muronorm 62.5 mg OD x 30 days", "strength_num": 62.5,
         "strength_unit": "mg", "form": "tablet", "dose_num": 1, "dose_unit_ucum": "{tbl}",
         "frequency_code": "OD", "duration_days": 30, "route": "oral",
         "instructions": "empty stomach"}  # fmt: skip
    assert line_text(f) == (
        "Tab Muronorm 62.5 mg OD x 30 days | strength 62.5 mg; form tablet; dose 1 {tbl}; "
        "frequency OD; 30 days; route oral; instructions empty stomach"
    )
    assert line_text({"local_text": "Tab Dolo"}) == "Tab Dolo"  # nothing else read


def test_the_recall_prompt_sends_the_extracted_lines_and_the_diagnoses():
    ln = _line(written="Tab Muronorm 62.5 mg OD | strength 62.5 mg; frequency OD")
    p = recall_prompt(["Hypothyroid", "RA"], [ln])
    assert "- Hypothyroid\n- RA" in p
    assert "[1] Tab Muronorm 62.5 mg OD | strength 62.5 mg; frequency OD" in p
    assert "brands" in p and "generics" in p and "India" in p
    # the diagnoses only decide between look-alikes: never a drug the name does not resemble
    assert "never propose a medicine the written name does not resemble" in p
    assert "- (none written)" in recall_prompt([], [ln])


def test_the_recall_answer_is_cleaned():
    assert _names(["Thyronorm", "thyronorm", " Eltroxin ", "", 5, None, "X", "A" * 61]) == [
        "Thyronorm",
        "Eltroxin",
    ]
    assert _names(["a1", "b22", "c33", "d44", "e55"]) == ["b22", "c33", "d44"]  # at most 3
    assert _names("Thyronorm") == [] and _names(None) == []  # not a list


def test_the_options_the_recall_pointed_to():
    rec = {"brands": ["Thyronorm"], "generics": ["Levothyroxine"]}
    assert from_recall(THYRONORM, rec)  # the brand named
    assert not from_recall(ELTROXIN, rec)  # a generic not in its composition as written
    assert from_recall(ELTROXIN, {"brands": [], "generics": ["Thyroxine"]})  # the salt named
    assert not from_recall(MERONORM, {"brands": ["Thyronorm"], "generics": ["Thyroxine"]})
    assert not from_recall(THYRONORM, {"brands": [], "generics": ["Thy"]})  # too short to count


def test_the_choose_prompt_without_recall_is_unchanged_and_with_it_has_the_context():
    ln = _line(options=[THYRONORM, MERONORM])
    before = choose_prompt([ln])
    assert '[1] first read as "Muronorm", strength 62.5. Options:\n' in before
    assert "diagnoses" not in before and "Extracted line" not in before
    ln.written = "Tab Muronorm 62.5 mg OD"
    after = choose_prompt([ln], ["Hypothyroid"])
    assert "diagnoses (use them only to decide between look-alike names): Hypothyroid." in after
    assert "Extracted line: Tab Muronorm 62.5 mg OD. Options:" in after


def test_recall_uses_the_agent_preamble_and_the_stub_answers_it_validly():
    schema = {"$id": "cdi:med_agent_recall.v1", "properties": {"lines": {}}}
    assert "Never add a value" not in with_schema("q", schema)
    out = StubBackend().generate("x", json_schema={"$id": "cdi:med_agent_recall.v1"})
    assert validate_schema(json.loads(out), "med_agent_recall.v1") == []
