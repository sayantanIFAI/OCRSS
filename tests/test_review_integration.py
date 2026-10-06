"""E0-S12: S8 review console API + page, against real PostgreSQL (no object store).

E7-S1: the real web app, signed in as a clinician reviewer. The reviewer recorded on every
decision is that session's user; a reviewer sent by the client is ignored."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.db import session_scope
from cdi_adapter.fhir.service import project_document
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.helpers import browser, signed_in
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

HELD_LINES_TAIL = ["Rx", "Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 50 mg OD"]


@pytest.fixture
def client(db_env):
    from cdi_adapter.webapp.app import app

    return signed_in(app, "reviewer")


def _me(client) -> str:
    return client.get("/api/me").json()["username"]


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
    r = client.post(f"/api/facts/{fid}/review", json={"action": "accept"})
    assert r.status_code == 200 and r.json()["review_state"] == "clinician_confirmed"
    with session_scope() as c:
        prov = repo.get_provenance(c, fid)
        audit = c.execute(text("SELECT actor, detail FROM audit_log WHERE entity = 'clinical_fact' "
                               "AND entity_id = :i"), {"i": fid}).mappings().one()  # fmt: skip
    me = _me(client)
    assert [(p["agent"], p["model_stack"]) for p in prov][-1] == (me, {"reviewer": "human"})
    assert audit["actor"] == me and audit["detail"]["action"] == "accept"
    # the same fact cannot be decided twice
    again = client.post(f"/api/facts/{fid}/review", json={"action": "reject"})
    assert again.status_code == 409


def test_correct_supersedes_and_reject_closes_the_task(seeded_scans, client):
    doc = _held_doc()
    facts = {f["local_text"]: f["id"] for f in _task_for(client, doc)["facts"]}
    met, aml = facts["Tab Metformin"], facts["Tab Amlodipine"]
    body = {"action": "correct", "corrections": {"local_text": "Tab Metformin 500"}}
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

    r = client.post(f"/api/facts/{aml}/review", json={"action": "reject"})
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
    # the reviewer's corrected strength shows in the prescribed medicine (2026-09-25)
    assert [m["medicationCodeableConcept"]["text"] for m in meds] == ["Metformin 5000 mg"]


@pytest.mark.parametrize(
    "body,status",
    [({"action": "approve"}, 422), ({"action": "correct", "corrections": {"patient_id": "x"}}, 422),
     ({"action": "correct", "corrections": {}}, 422),
     ({"action": "accept", "note": "x" * 2001}, 422)],
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


# ------------------------------------------------------------------ E7-S1 (DoD-SEC misuse cases)


def test_a_spoofed_reviewer_is_ignored_the_session_user_signs(client):
    doc = _held_doc()
    fid = _task_for(client, doc)["facts"][0]["id"]
    r = client.post(f"/api/facts/{fid}/review",
                    json={"action": "accept", "reviewer": "dr.mallory"})  # fmt: skip
    assert r.status_code == 200
    with session_scope() as c:
        agents = [p["agent"] for p in repo.get_provenance(c, fid)]
        actors = c.execute(text("SELECT actor FROM audit_log WHERE entity = 'clinical_fact' "
                                "AND entity_id = :i"), {"i": fid}).scalars().all()  # fmt: skip
    assert agents[-1] == _me(client) and actors == [_me(client)]
    assert "dr.mallory" not in agents and "dr.mallory" not in actors


@pytest.mark.parametrize("role,status", [(None, 401), ("intake", 403)])
def test_the_review_console_needs_a_clinician_session(db_env, role, status):
    from cdi_adapter.webapp.app import app

    doc = _held_doc()
    with session_scope() as c:
        fact = repo.get_facts_for_document(c, doc)[0]
    fid, before = str(fact["id"]), fact["review_state"]
    c = browser(app) if role is None else signed_in(app, role)
    for method, path in (("get", "/api/review/tasks"), ("get", f"/api/review/facts/{fid}"),
                         ("post", f"/api/facts/{fid}/review")):  # fmt: skip
        kw = {"json": {"action": "accept", "reviewer": "dr.sen"}} if method == "post" else {}
        assert getattr(c, method)(path, **kw).status_code == status, path
    page = c.get("/review", follow_redirects=False)
    assert page.status_code == (303 if role is None else 403)  # a browser is sent to /login
    with session_scope() as s:  # nothing was decided
        state = s.execute(text("SELECT review_state FROM clinical_fact WHERE id = :i"),
                          {"i": fid}).scalar_one()  # fmt: skip
    assert state == before


def test_job_generate_edits_are_signed_by_the_session_user(seeded_scans, client):
    """The Generate EMR edits (keep / drop / correct) are clinical decisions too (§7.2)."""
    import uuid

    from cdi_adapter.webapp.jobs import DocState, Job
    from cdi_adapter.webapp.jobstore import PgJobStore

    doc = _held_doc()
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
    fid, patient = str(facts[0]["id"]), str(facts[0]["patient_id"])
    job = Job(id=uuid.uuid4().hex, docs=[DocState(filename="rx.pdf", document_id=doc)],
              state="review", patient_id=patient)  # fmt: skip
    st = PgJobStore()  # E10-S9: jobs live in web_job; this one has finished at review
    st.insert(job, [], actor="counter1", accepted_by="test")
    st.claim(job.id, "test", 30)
    st.finish(job, "test")
    edits = [{"fact_id": fid, "action": "keep", "corrections": {"local_text": "Tab X 1 mg"}}]
    r = client.post(f"/api/jobs/{job.id}/generate",
                    json={"edits": edits, "reviewer": "dr.mallory"})  # fmt: skip
    assert r.status_code == 200, r.text
    with session_scope() as c:
        new = c.execute(text("SELECT id FROM clinical_fact WHERE supersedes = :i"),
                        {"i": fid}).scalar_one()  # fmt: skip
        agent = repo.get_provenance(c, new)[-1]["agent"]
        actor = c.execute(text("SELECT actor FROM audit_log WHERE entity = 'clinical_fact' "
                               "AND entity_id = :i"), {"i": fid}).scalar_one()  # fmt: skip
    assert agent == actor == _me(client) != "dr.mallory"
