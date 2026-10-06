"""The medication agent end to end on PostgreSQL (backlog E12-S4 / E12-S5, ADR-0003).

A prescription whose handwriting was misread ("Muronorm" for Thyronorm, "Shaz" for Saaz) is run
through the agent with a scripted model: the agent re-reads each line with the diagnoses, asks the
catalogue for a shortlist, has the model choose, guards the choice and records suggestions. It
never changes a fact; every step is in ``agent_trace``; a reviewer's correction to the suggested
name is identified exactly by the catalogue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.agent.medication import latest_for_document, run_medication_agent
from cdi_adapter.catalogue import service as cat
from cdi_adapter.db import session_scope
from cdi_adapter.ml.client import MLClient

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).parent / "data" / "catalogue_sample_synthetic.csv"
RX = [
    "CareFlow Polyclinic",
    "Name: Agent Test   Age/Sex: 73 Y / F",
    "Date: 23/07/2026",
    "Diagnosis: Hypothyroid; Rheumatoid arthritis",
    "Rx",
    "Tab Muronorm 62.5 mg OD x 30 days",
    "Tab Shaz 500 mg 1-0-1 x 30 days",
    "Tab Dolo 650 mg SOS",
]


class ScriptedAgentModel(MLClient):
    """Answers the agent's three schemas like a model that can read the page and knows medicines.

    ``pick``: first reading -> product it chooses; ``claims``: an ingredient -> the diagnosis it
    claims (right or wrong); ``confirms``: ingredients whose claim it confirms when asked yes/no.
    """

    backend, model = "scripted", "scripted-agent-model"

    def __init__(self, pick: dict[str, str], claims: dict[str, str],
                 confirms: set[str]) -> None:  # fmt: skip
        self.pick, self.claims, self.confirms, self.calls = pick, claims, confirms, []

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
        sid = (json_schema or {}).get("$id", "")
        self.calls.append(sid)
        if sid == "cdi:med_agent_fit.v1":
            lines = []
            for n, contains in re.findall(r"^\[(\d+)\] .+? - contains (.+)$", prompt, re.M):
                dx = next((d for ing, d in self.claims.items() if ing in contains), None)
                lines.append({"line": int(n), "treats": dx, "why": "scripted"})
            return json.dumps({"lines": lines, "confidence": 0.8})
        if sid == "cdi:med_agent_verify.v1":
            pairs = [{"pair": int(n), "answer": "yes" if any(i in m for i in self.confirms)
                      else "no"}
                     for n, m in re.findall(r"^\[(\d+)\] (.+) for .+$", prompt, re.M)]  # fmt: skip
            return json.dumps({"pairs": pairs, "confidence": 0.8})
        lines = []
        for block in re.split(r"\n\n(?=\[\d+\])", prompt):
            m = re.match(r"\[(\d+)\]", block)
            if not m:
                continue
            n, want = int(m.group(1)), None
            for read_as, product in self.pick.items():
                if f'"{read_as}"' in block:
                    want = product
            opt = re.search(rf"(\d+)\) {re.escape(want)}", block) if want else None
            lines.append({"line": n, "choice": int(opt.group(1)) if opt else 0,
                          "evidence": want.split()[0] if want else None,
                          "rationale": "as written", "confidence": 0.9})  # fmt: skip
        return json.dumps({"lines": lines, "confidence": 0.9})


@pytest.fixture(autouse=True)
def _no_recall(monkeypatch):
    """This module checks the agent without the recall step (``agent_recall`` false), i.e. the
    agent exactly as it was; test_agent_recall_integration checks the recall step."""
    from cdi_adapter.config import get_settings

    monkeypatch.setenv("CDI_AGENT_RECALL", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def rx(db_env):
    from cdi_adapter.terminology.service import bind_document
    from cdi_adapter.validate.service import validate_document
    from tests.test_extract_integration import _doc_with_ocr, _write

    with session_scope() as c:
        cat.import_csv(c, SAMPLE, source="test-1mg", actor="pytest")
    doc, blocks = _doc_with_ocr(RX)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    yield doc
    with session_scope() as c:
        c.execute(text("DELETE FROM medication_catalogue_match"))
        c.execute(text("DELETE FROM med_catalogue"))
        c.execute(text("DELETE FROM med_catalogue_import"))


def _meds(doc):
    with session_scope() as c:
        return {f["drug_text"] or f["local_text"]: f for f in repo.get_facts_for_document(c, doc)
                if f["fact_type"] == "medication" and f["is_current"]}  # fmt: skip


def _model(meds):
    names = list(meds)
    muro, shaz = next(n for n in names if "Muronorm" in n), next(n for n in names if "Shaz" in n)
    # paracetamol is claimed for arthritis (a stretch the 7B makes); the yes/no check refuses it
    return ScriptedAgentModel(
        {muro: "Thyronorm 62.5mcg Tablet", shaz: "Saaz Tablet"},
        {"Thyroxine": "Hypothyroid", "Sulfasalazine": "Rheumatoid arthritis",
         "Paracetamol": "Rheumatoid arthritis"},
        {"Thyroxine", "Sulfasalazine"},
    )  # fmt: skip


def test_the_agent_suggests_the_written_product_from_the_diagnoses(rx):
    meds = _meds(rx)
    assert len(meds) == 3
    before = {str(f["id"]): f["review_state"] for f in meds.values()}
    model = _model(meds)
    out = run_medication_agent(rx, client=model)
    assert model.calls == [
        "cdi:med_agent_choice.v1",
        "cdi:med_agent_fit.v1",
        "cdi:med_agent_verify.v1",
    ]
    assert out["lines"] == 3 and out["suggested"] == 2
    with session_scope() as c:
        got = latest_for_document(c, rx)
        by_text = {s["read_as"]: s for s in got["by_fact"].values()}
        after = {str(f["id"]): f["review_state"] for f in repo.get_facts_for_document(c, rx)
                 if f["fact_type"] == "medication" and f["is_current"]}  # fmt: skip
        # the medication agent's own steps (the gate's E4-S8 cascade logs its steps here too)
        trace = c.execute(text("SELECT step FROM agent_trace WHERE document_id = :d "
                               "AND agent = 'medication_agent.v1' ORDER BY seq"),
                          {"d": rx}).scalars().all()  # fmt: skip
    assert got["run"]["status"] == "ok"
    muro = next(v for k, v in by_text.items() if "Muronorm" in k)
    assert (muro["status"], muro["product"]["name"], muro["treats"]) == (
        "suggested",
        "Thyronorm 62.5mcg Tablet",
        "Hypothyroid",
    )
    shaz = next(v for k, v in by_text.items() if "Shaz" in k)
    assert (shaz["status"], shaz["product"]["name"]) == ("suggested", "Saaz Tablet")
    assert shaz["treats"] == "Rheumatoid arthritis"
    dolo = next(v for k, v in by_text.items() if "Dolo" in k)
    # exactly identified by the catalogue: only the fit check; the claimed link to arthritis was
    # not confirmed by the second opinion, so it is flagged "no matching diagnosis"
    assert dolo["status"] == "confirmed" and dolo["fits_diagnosis"] is False
    assert dolo["treats"] is None and out["no_matching_diagnosis"] == 1
    # ADR-0003 rule 3: advisory only, no fact or review state changed
    assert after == before
    # E12-S5: the whole decision sequence is in the trace, in order
    assert trace == [
        "shortlist",
        "shortlist",
        "choose",
        "guard",
        "guard",
        "fit",
        "verify",
        "suggest",
    ]


def test_a_reviewers_correction_to_the_suggestion_is_identified_exactly(rx):
    from cdi_adapter.webapp.review import ReviewDecision, decide

    meds = _meds(rx)
    run_medication_agent(rx, client=_model(meds))
    muro = next(f for k, f in meds.items() if "Muronorm" in k)
    with session_scope() as c:
        res = decide(c, muro["id"], ReviewDecision(action="correct", corrections={
            "drug_text": "Thyronorm 62.5mcg Tablet"}), "dr-test")  # fmt: skip
        match = cat.matches_for_facts(c, [res["result_fact_id"]])[res["result_fact_id"]]
        got = latest_for_document(c, rx)
    assert (match["status"], match["method"]) == ("matched", "exact")
    assert match["product_name"] == "Thyronorm 62.5mcg Tablet"
    assert str(muro["id"]) in got["by_fact"]  # the suggestion stays with the replaced fact


def test_the_agent_is_off_without_a_catalogue_or_when_disabled(db_env, monkeypatch):
    from cdi_adapter.config import get_settings
    from tests.test_extract_integration import _doc_with_ocr, _write

    doc, blocks = _doc_with_ocr(RX)
    _write(doc, blocks)
    assert run_medication_agent(doc, client=ScriptedAgentModel({}, {}, set())) == {
        "skipped": "no catalogue"
    }
    monkeypatch.setenv("CDI_AGENT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert run_medication_agent(doc) == {"skipped": "disabled"}
    finally:
        monkeypatch.delenv("CDI_AGENT_ENABLED")
        get_settings.cache_clear()
