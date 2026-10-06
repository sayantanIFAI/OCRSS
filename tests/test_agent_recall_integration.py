"""The medication agent WITH the recall step (owner request 2026-09-28) end to end on PostgreSQL:
each medicine line as extracted and the prescription's diagnoses go to the model, whose own
knowledge names the brands / generics; the catalogue shortlist is built from those too, the
choice and the guard are unchanged, and the Reviewer's Replace lists the options the recall led
to first. The scripted model and the prescription are test_agent_integration's."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from cdi_adapter.agent.medication import latest_for_document, run_medication_agent
from cdi_adapter.db import session_scope
from tests.test_agent_integration import ScriptedAgentModel, _meds, _model, rx  # noqa: F401

pytestmark = pytest.mark.integration


class RecallingModel(ScriptedAgentModel):
    """The scripted agent model that also answers the recall step: ``knows`` maps a word of the
    extracted line to (brands, generics) the model's knowledge names for it."""

    def __init__(self, base: ScriptedAgentModel, knows: dict[str, tuple[list, list]]) -> None:
        super().__init__(base.pick, base.claims, base.confirms)
        self.knows, self.prompts = knows, {}

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
        sid = (json_schema or {}).get("$id", "")
        self.prompts[sid] = prompt
        if sid != "cdi:med_agent_recall.v1":
            return super().generate(prompt, image_png, max_tokens=max_tokens,
                                    json_schema=json_schema)  # fmt: skip
        self.calls.append(sid)
        lines = []
        for n, written in re.findall(r"^\[(\d+)\] (.+)$", prompt, re.M):
            hit = next((v for k, v in self.knows.items() if k in written), ([], []))
            lines.append({"line": int(n), "brands": hit[0], "generics": hit[1],
                          "reason": "spelling, strength and a listed diagnosis"})  # fmt: skip
        return json.dumps({"lines": lines, "confidence": 0.7})


@pytest.fixture
def recalling(rx):  # noqa: F811
    model = RecallingModel(_model(_meds(rx)), {
        "Muronorm": (["Thyronorm"], ["Thyroxine"]),
        "Shaz": (["Saaz"], ["Sulfasalazine"]),
    })  # fmt: skip
    return rx, model


def test_the_extracted_lines_and_diagnoses_go_to_the_model_and_widen_the_shortlist(recalling):
    doc, model = recalling
    out = run_medication_agent(doc, client=model)
    assert model.calls == [
        "cdi:med_agent_recall.v1",
        "cdi:med_agent_choice.v1",
        "cdi:med_agent_fit.v1",
        "cdi:med_agent_verify.v1",
    ]
    recall = model.prompts["cdi:med_agent_recall.v1"]
    # the diagnoses and each open line as extracted (with its details) are in the question
    assert "- Hypothyroid" in recall and "- Rheumatoid arthritis" in recall
    assert re.search(r"^\[\d\] Tab Muronorm \| strength 62\.5 mg; .*frequency OD; 30 days$",
                     recall, re.M)  # fmt: skip
    assert "Dolo" not in recall  # identified exactly by the catalogue: nothing to recall
    choose = model.prompts["cdi:med_agent_choice.v1"]
    assert "diagnoses (use them only" in choose and "Extracted line: Tab Shaz" in choose
    assert out["suggested"] == 2
    with session_scope() as c:
        got = latest_for_document(c, doc)["by_fact"]
        # the medication agent's own steps (the gate's E4-S8 cascade logs its steps here too)
        trace = c.execute(text("SELECT step, input FROM agent_trace WHERE document_id = :d "
                               "AND agent = 'medication_agent.v1' ORDER BY seq"),
                          {"d": doc}).all()  # fmt: skip
    by_text = {s["read_as"]: s for s in got.values()}
    muro = next(v for k, v in by_text.items() if "Muronorm" in k)
    assert muro["status"] == "suggested" and muro["product"]["name"] == "Thyronorm 62.5mcg Tablet"
    assert muro["recalled"]["brands"] == ["Thyronorm"]
    assert muro["knowledge_options"][0] == "Thyronorm 62.5mcg Tablet"
    shaz = next(v for k, v in by_text.items() if "Shaz" in k)
    assert "Saaz Tablet" in shaz["knowledge_options"]
    assert [t[0] for t in trace][:2] == ["recall", "shortlist"]
    assert trace[1][1]["recalled"]["generics"]  # the shortlist was asked with the recall


def test_replace_lists_the_options_from_the_agents_knowledge_first(recalling):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    doc, model = recalling
    run_medication_agent(doc, client=model)
    muro = next(f for k, f in _meds(doc).items() if "Muronorm" in k)
    d = signed_in(app, "reviewer").get(f"/api/reviewer/facts/{muro['id']}/replacements").json()
    first = d["agent"][0]
    assert first["name"] == "Thyronorm 62.5mcg Tablet"
    # the suggestion is also what the model's own knowledge named for the line
    assert first["why"].startswith("agent suggests · also from its knowledge and the diagnoses")
    assert "spelling, strength and a listed diagnosis" in first["why"]
    assert all(o["why"] == "agent's options" for o in d["agent"][1:])


def test_a_model_that_knows_nothing_changes_nothing(recalling):
    doc, model = recalling
    model.knows = {}  # empty lists for every line
    out = run_medication_agent(doc, client=model)
    assert out["suggested"] == 2  # the reading alone still finds them, as before
    with session_scope() as c:
        got = latest_for_document(c, doc)["by_fact"]
    muro = next(v for v in got.values() if "Muronorm" in v["read_as"])
    assert (
        muro["recalled"]
        == {"brands": [], "generics": [], "reason": ("spelling, strength and a listed diagnosis")}
        and muro["knowledge_options"] == []
    )
