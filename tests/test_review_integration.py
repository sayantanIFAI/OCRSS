"""E0-S12: S8 review console API + page, against real PostgreSQL (no object store)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.fhir.service import project_document
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from cdi_adapter.webapp import review, review_page
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

HELD_LINES_TAIL = ["Rx", "Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 50 mg OD"]


@pytest.fixture
def client(db_env):
    app = FastAPI()
    app.include_router(review.router)
    app.include_router(review_page.router)
    return TestClient(app)


def _held_doc():
    doc, blocks = _doc_with_ocr(_rx()[:4] + HELD_LINES_TAIL)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    return doc


def _task_for(client, doc):
    return next(t for t in client.get("/api/review/tasks").json() if t["document_id"] == doc)


def test_tasks_list_held_facts(client):
    doc = _held_doc()
    task = _task_for(client, doc)
    assert task["kind"] == "dose_check" and task["status"] == "open"
    assert sorted(f["local_text"] for f in task["facts"]) == ["Tab Amlodipine", "Tab Metformin"]


def test_fact_detail_has_provenance_bbox_blocks_page_url_findings(client):
    doc = _held_doc()
    fid = _task_for(client, doc)["facts"][0]["id"]
    d = client.get(f"/api/review/facts/{fid}").json()
    (p,) = d["provenance"]
    assert p["agent"] == "system" and len(p["bbox_union"]) == 4
    assert p["ocr_blocks"][0]["text"].startswith("Tab ")
    assert p["page_image_url"] == f"/api/documents/{doc}/pages/1"
    assert float(d["medication"]["strength_num"]) in (5000, 50)  # numeric -> JSON string
    assert any(f["code"] == "med-dose-ceiling" for f in d["findings"])


def test_accept_confirms_signs_provenance_and_audits(client):
    doc = _held_doc()
    fid = _task_for(client, doc)["facts"][0]["id"]
    r = client.post(f"/api/facts/{fid}/review", json={"action": "accept", "reviewer": "dr.sen"})
    assert r.status_code == 200 and r.json()["review_state"] == "clinician_confirmed"
    with session_scope() as c:
        prov = repo.get_provenance(c, fid)
        audit = c.execute(text("SELECT actor, detail FROM audit_log WHERE entity = 'clinical_fact' "
                               "AND entity_id = :i"), {"i": fid}).mappings().one()  # fmt: skip
    assert [(p["agent"], p["model_stack"]) for p in prov][-1] == ("dr.sen", {"reviewer": "human"})
    assert audit["actor"] == "dr.sen" and audit["detail"]["action"] == "accept"
    # the same fact cannot be decided twice
    again = client.post(f"/api/facts/{fid}/review", json={"action": "reject"})
    assert again.status_code == 409


def test_correct_supersedes_and_reject_closes_the_task(client):
    doc = _held_doc()
    facts = {f["local_text"]: f["id"] for f in _task_for(client, doc)["facts"]}
    met, aml = facts["Tab Metformin"], facts["Tab Amlodipine"]
    body = {"action": "correct", "reviewer": "dr.sen",
            "corrections": {"local_text": "Tab Metformin 500"}}  # fmt: skip
    r = client.post(f"/api/facts/{met}/review", json=body)
    new_id = r.json()["result_fact_id"]
    assert (
        r.json()["review_state"] == "corrected" and new_id != met and r.json()["tasks_closed"] == 0
    )
    with session_scope() as c:
        old = (
            c.execute(text("SELECT * FROM clinical_fact WHERE id=:i"), {"i": met}).mappings().one()
        )
        new = c.execute(text("SELECT * FROM clinical_fact WHERE id=:i"),
                        {"i": new_id}).mappings().one()  # fmt: skip
        med = c.execute(text("SELECT strength_num FROM medication_detail WHERE fact_id=:i"),
                        {"i": new_id}).scalar_one()  # fmt: skip
        n_prov = len(repo.get_provenance(c, new_id))
    assert (old["is_current"], new["is_current"], str(new["supersedes"])) == (False, True, met)
    assert new["local_text"] == "Tab Metformin 500" and med == 5000  # detail copied as-is
    assert n_prov == 2  # the original evidence + the reviewer's signature

    r = client.post(f"/api/facts/{aml}/review", json={"action": "reject", "reviewer": "dr.sen"})
    assert r.json()["review_state"] == "rejected" and r.json()["tasks_closed"] == 1
    with session_scope() as c:
        rej = (
            c.execute(text("SELECT * FROM clinical_fact WHERE id=:i"), {"i": aml}).mappings().one()
        )
        # the held-fact task (a separate low-priority audit_sample task may also exist, §14)
        task = c.execute(text("SELECT status FROM review_task WHERE document_id=:d "
                              "AND kind <> 'audit_sample'"), {"d": doc}).scalar_one()  # fmt: skip
    assert (rej["is_current"], rej["clinical_status"]) == (False, "entered-in-error")
    assert task == "closed"

    # live re-projection now asserts the corrected medication, never the rejected one
    out = project_document(doc, persist=False)
    meds = [e["resource"] for e in out["bundle"]["entry"]
            if e["resource"]["resourceType"] == "MedicationRequest"]  # fmt: skip
    assert out["status"] == "ready_to_share"
    assert [m["medicationCodeableConcept"]["text"] for m in meds] == ["Metformin"]


@pytest.mark.parametrize(
    "body,status",
    [({"action": "approve"}, 422), ({"action": "correct", "corrections": {"patient_id": "x"}}, 422),
     ({"action": "correct", "corrections": {}}, 422), ({"action": "accept", "reviewer": ""}, 422)],
)  # fmt: skip
def test_bad_decisions_are_rejected(client, body, status):
    doc = _held_doc()
    fid = _task_for(client, doc)["facts"][0]["id"]
    assert client.post(f"/api/facts/{fid}/review", json=body).status_code == status
    with session_scope() as c:  # nothing changed
        assert c.execute(text("SELECT review_state FROM clinical_fact WHERE id = :i"),
                         {"i": fid}).scalar_one() == "in_review"  # fmt: skip


def test_unknown_fact_is_404(client):
    assert client.get("/api/review/facts/00000000-0000-0000-0000-000000000000").status_code == 404


def test_review_page_renders(client):
    r = client.get("/review")
    assert r.status_code == 200 and "Review queue" in r.text and "/api/review/tasks" in r.text
    assert "cdn" not in r.text.lower()  # no external scripts
