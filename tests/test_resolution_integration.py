"""E4-S8 on the real S6 gate (PostgreSQL): every gated fact is resolved by the cascade, logged as
an E12-S5 agent step; an auto-accepted fact stores its level and evidence matrix; the S8 console
shows them; the cascade can only hold a fact, never accept one the gate holds."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from cdi_adapter.webapp.review import ReviewDecision, decide
from tests.test_extract_integration import _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration


def _doc(lines: list[str], engine: str = "rapidocr") -> tuple[str, list[dict]]:
    with session_scope() as c:
        sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        doc = repo.insert_source_document(c, sha256=sha, filename="rx.pdf",
                                          mime_type="application/pdf", byte_size=1_000_000,
                                          storage_key="unused")  # fmt: skip
        page = repo.insert_document_page(c, document_id=doc, page_no=1, storage_key="p",
                                         thumb_key="t", width=1654, height=2339, dpi=200,
                                         preproc={})  # fmt: skip
        repo.insert_ocr_blocks(c, [
            {"document_id": doc, "page_id": page, "block_type": "line", "text": t,
             "bbox": [100, 100 + 40 * i, 1200, 130 + 40 * i], "polygon": None,
             "ocr_conf": 0.96, "reading_order": i, "engine": engine, "pipeline_run_id": None,
             "region_kind": "handwritten" if engine == "vlm" else "printed"}
            for i, t in enumerate(lines)
        ])  # fmt: skip
        repo.insert_classification(c, document_id=doc, run_id=None, doc_type="prescription",
                                   specialty=None, languages=["en"], is_handwritten=False,
                                   page_spans=[], confidence=0.95, method="heuristic")  # fmt: skip
        blocks = repo.get_ocr_blocks(c, doc)
    return str(doc), blocks


def _decisions(c, doc: str) -> list[dict]:
    return [dict(r) for r in c.execute(
        text("SELECT t.fact_id, t.seq, t.step, t.tool, t.input, t.output, r.stage "
             "FROM agent_trace t JOIN pipeline_run r ON r.id = t.run_id "
             "WHERE t.document_id = :d AND t.agent = 'resolution-cascade' ORDER BY t.seq"),
        {"d": doc},
    ).mappings()]  # fmt: skip


def test_every_gated_fact_is_resolved_and_logged_without_changing_the_gate(db_env):
    doc, blocks = _doc(_rx())
    _write(doc, blocks)
    bind_document(doc)
    out = validate_document(doc)
    assert out["auto_accepted"] == 7 and out["in_review"] == 0  # the §8.2 example, as before
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
        logged = _decisions(c, doc)
        cands = c.execute(
            text("SELECT c.fact_id, c.resolved_by_level, c.evidence FROM verified_fact v "
                 "JOIN interpretation_candidate c ON c.id = v.candidate_id "
                 "WHERE v.document_id = :d AND v.verification_method = 'auto_gate'"), {"d": doc},
        ).mappings().all()  # fmt: skip
    # one E12-S5 agent step per gated fact, in the validate run
    assert len(logged) == len(facts) and {x["stage"] for x in logged} == {"validate"}
    assert all(x["step"] == "resolve" and x["tool"] == "cascade.v1" for x in logged)
    assert all(x["output"]["outcome"] == "accept" for x in logged)
    # each auto-accepted fact stores the level that resolved it and its per-signal matrix
    assert len(cands) == 7
    for cand in cands:
        assert cand["resolved_by_level"] in ("L0", "L1", "L2", "L3")
        tiers = {s["tier"] for s in cand["evidence"]["matrix"]}
        assert 1 in tiers  # the recognizer reading
        assert cand["evidence"]["cascade"] == "cascade.v1"
    coded = [cd for cd in cands if any(s["tier"] == 2 for s in cd["evidence"]["matrix"])]
    assert coded  # terminology bindings are tier-2 evidence
    trace = logged[0]["output"]["trace"]
    assert trace[0]["step"] == "input" and {"tier_not_built", "resolved"} <= {
        t["step"] for t in trace
    }
    # the cheapest level resolved it: the cascade stopped there, nothing more expensive ran
    assert [t["level"] for t in trace if t["step"] == "resolved"][0] in ("L0", "L1", "L2", "L3")
    assert not [t for t in trace if t.get("level") in ("L4", "L5", "L6", "L7")]


def test_held_facts_stay_held_and_their_decision_is_logged(db_env):
    doc, blocks = _doc(_rx()[:4] + ["Rx", "Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"])
    _write(doc, blocks)
    bind_document(doc)
    out = validate_document(doc)
    assert out["in_review"] == 1  # the over-ceiling dose, held by the S6 rules as before
    with session_scope() as c:
        held = next(f for f in repo.get_facts_for_document(c, doc)
                    if f["review_state"] == "in_review")  # fmt: skip
        codes = [x["code"] for x in held["review_note"]["findings"]]
    assert "cascade-review" not in codes  # the cascade never adds itself to a held fact


def test_the_cascade_holds_what_no_validated_level_resolved(db_env):
    """A handwritten reading (VLM) is not validated for any cheap level: even with a confidence
    the gate would auto-accept, the fact goes to review, with the cause recorded."""
    doc, blocks = _doc(_rx(), engine="vlm")
    _write(doc, blocks)
    bind_document(doc)
    with session_scope() as c:  # what the gate would auto-accept, were it printed
        c.execute(text("UPDATE clinical_fact SET confidence_overall = 0.999 "
                       "WHERE :d = ANY(source_doc_ids)"), {"d": doc})  # fmt: skip
    out = validate_document(doc)
    assert out["auto_accepted"] == 0
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
        logged = _decisions(c, doc)
    held = [f for f in facts if any(x["code"] == "cascade-review"
                                    for x in f["review_note"]["findings"])]  # fmt: skip
    assert held and all(f["review_state"] == "in_review" for f in held)
    x = next(r for r in logged if r["fact_id"] == held[0]["id"])
    assert x["output"]["outcome"] == "review" and x["output"]["resolved_by_level"] == "L8"
    assert any(t["step"] == "not_validated" for t in x["output"]["trace"])
    skipped = [t["level"] for t in x["output"]["trace"] if t["step"] == "skip"]
    assert skipped == [
        "L4",
        "L5",
        "L6",
        "L7",
    ]  # escalated past levels not built: skipped, not faked
    assert held[0]["review_note"]["resolution"]["level"] == "L8"


def test_the_s8_console_shows_the_level_and_the_evidence_matrix(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    doc, blocks = _doc(_rx()[:4] + ["Rx", "Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"])
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
    accepted = next(f for f in facts if f["review_state"] == "auto_accepted")
    held = next(f for f in facts if f["review_state"] == "in_review")
    client = signed_in(app, "reviewer")
    r = client.get(f"/api/review/facts/{accepted['id']}").json()["resolution"]
    assert r["decision"]["outcome"] == "accept"
    assert r["decision"]["resolved_by_level"] in ("L0", "L1", "L2", "L3")
    assert r["decision"]["level_name"] and r["decision"]["matrix"]
    assert [v["verification_method"] for v in r["verified"]] == ["auto_gate"]
    # a reviewer's decision is verified at L8
    with session_scope() as c:
        decide(c, held["id"], ReviewDecision(action="accept"), "dr-e4s8")
    r = client.get(f"/api/review/facts/{held['id']}").json()["resolution"]
    assert [(v["verification_method"], v["resolved_by_level"], v["reviewer_id"])
            for v in r["verified"]] == [("reviewer", "L8", "dr-e4s8")]  # fmt: skip
    page = client.get("/review").text
    assert "resolutionRows" in page and "Evidence matrix" in page
