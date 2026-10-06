"""E0-S14: workbasket -> worklist -> promote -> canonical bundle, and /admin (real PostgreSQL)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cdi_adapter.canon.repo import create_rv_item_for_document
from cdi_adapter.db import session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db_env):
    from cdi_adapter.webapp.app import app

    return TestClient(app)


def _item_with_held_facts(extra_lines):
    lines = _rx()[:4] + ["Rx"] + extra_lines
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        item = create_rv_item_for_document(c, doc, job_id="job-t")
        again = create_rv_item_for_document(c, doc, job_id="job-t")
    assert again == item  # idempotent per document
    return doc, str(item)


def _post(client, url, **body):
    return client.post(url, json=body)


def test_full_workbasket_flow_promotes_and_builds_canonical_bundle(client):
    doc, item = _item_with_held_facts(["Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD",
                                       "BP: 138/86 mmHg"])  # fmt: skip
    wb = {i["id"]: i for i in client.get("/api/reviewer/workbasket").json()}
    assert wb[item]["held"] == 1 and wb[item]["elements"] == 5  # 2 dx, 2 meds, 1 BP

    # an item is claimed into a worklist before it is decided (§4.5)
    assert (
        _post(client, f"/api/reviewer/items/{item}/approve", reviewer="dr.sen").status_code == 409
    )
    assert _post(client, f"/api/reviewer/items/{item}/claim", reviewer="dr.sen").status_code == 200
    assert item not in {i["id"] for i in client.get("/api/reviewer/workbasket").json()}
    worklist = client.get("/api/reviewer/worklist", params={"reviewer": "dr.sen"}).json()
    assert [i["id"] for i in worklist] == [item]
    assert _post(client, f"/api/reviewer/items/{item}/claim", reviewer="dr.rao").status_code == 409

    # the reviewer works the held fact (correct it), then approves
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    held = next(e for e in els if e["state"] == "held")
    r = _post(client, f"/api/facts/{held['fact']['id']}/review", action="correct",
              reviewer="dr.sen", corrections={"local_text": "Tab Metformin 500"})  # fmt: skip
    assert r.status_code == 200
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    corrected = next(e for e in els if e["fact_id"] == held["fact_id"])
    assert (
        corrected["state"] == "corrected" and corrected["fact"]["local_text"] == "Tab Metformin 500"
    )

    r = _post(client, f"/api/reviewer/items/{item}/approve", reviewer="dr.sen")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["promoted"] == {"cn_condition": 2, "cn_medication": 2, "cn_vital_sign": 1}
    assert out["artifact_type"] == "PrescriptionRecord"
    assert out["status"] == "ready_to_share" and out["validation"]["valid"] is True

    with session_scope() as c:
        status = c.execute(
            text("SELECT status FROM rv_item WHERE id = :i"), {"i": item}
        ).scalar_one()
        actions = c.execute(text("SELECT action FROM rv_action WHERE rv_item_id = :i ORDER BY at"),
                            {"i": item}).scalars().all()  # fmt: skip
        decisions = sorted(c.execute(text(
            "SELECT v.decision FROM ai_clinical_verification v JOIN ai_suggestion s "
            "ON s.id = v.ai_suggestion_id JOIN ai_event e ON e.id = s.ai_event_id "
            "WHERE e.rv_item_id = :i"), {"i": item}).scalars())  # fmt: skip
        bp = c.execute(text("SELECT value_text FROM cn_vital_sign WHERE rv_item_id = :i"),
                       {"i": item}).scalar_one()  # fmt: skip
    assert status == "approved" and actions == ["create", "claim", "approve", "promote"]
    assert decisions == ["accepted"] * 4 + ["corrected"] and bp == "138/86"

    b = client.get(f"/api/reviewer/items/{item}/bundle").json()
    types = [e["resource"]["resourceType"] for e in b["bundle"]["entry"]]
    assert types[0] == "Composition" and types.count("MedicationRequest") == 2
    assert b["status"] == "ready_to_share"
    assert (
        _post(client, f"/api/reviewer/items/{item}/approve", reviewer="dr.sen").status_code == 409
    )


def test_approving_with_a_held_fact_never_promotes_it(client):
    _, item = _item_with_held_facts(["Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"])
    _post(client, f"/api/reviewer/items/{item}/claim", reviewer="dr.x")
    r = _post(client, f"/api/reviewer/items/{item}/approve", reviewer="dr.y")  # §4.5: approve
    assert r.status_code == 200
    assert r.json()["promoted"] == {"cn_condition": 2, "cn_medication": 1}  # held Metformin out
    with session_scope() as c:
        drugs = c.execute(text("SELECT drug_text FROM cn_medication WHERE rv_item_id = :i"),
                          {"i": item}).scalars().all()  # fmt: skip
        n_verif = c.execute(text(
            "SELECT count(*) FROM ai_clinical_verification v JOIN ai_suggestion s "
            "ON s.id = v.ai_suggestion_id JOIN ai_event e ON e.id = s.ai_event_id "
            "WHERE e.rv_item_id = :i"), {"i": item}).scalar_one()  # fmt: skip
    assert drugs == ["Amlodipine"]
    assert n_verif == 3  # nobody decided the held fact: no verification is recorded for it


def test_lab_item_promotes_into_cn_lab_result_test_code(client):
    lines = ["Name: Canonlab Qz  Age: 50 Y", "Date: 02/09/2026", "LABORATORY REPORT",
             "HbA1c 7.8 % 4.0-5.6 H", "Serum Creatinine 1.1 mg/dL 0.6-1.3"]  # fmt: skip
    doc, blocks = _doc_with_ocr(lines, doc_type="lab_report")
    out = _write(doc, blocks, doc_type="lab_report")
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        item = str(create_rv_item_for_document(c, doc))
        mpi = c.execute(text("SELECT mpi_id FROM patient_identity WHERE id = :p"),
                        {"p": out["patient_id"]}).scalar_one()  # fmt: skip
    _post(client, f"/api/reviewer/items/{item}/claim", reviewer="dr.lab")
    r = _post(client, f"/api/reviewer/items/{item}/approve", reviewer="dr.lab")
    assert r.json()["promoted"] == {"cn_lab_result": 2}
    page = client.get("/admin", params={"mpi": mpi})  # patient_graph reads test_code (§4.5 fix)
    assert page.status_code == 200
    assert "4548-4" in page.text and "2160-0" in page.text and "DiagnosticReportRecord" in page.text


def test_reject_item(client):
    _, item = _item_with_held_facts(["Tab Metformin 5000 mg 1-1-1"])
    _post(client, f"/api/reviewer/items/{item}/claim", reviewer="dr.x")
    r = _post(client, f"/api/reviewer/items/{item}/reject", reviewer="dr.x", reason="wrong patient")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    with session_scope() as c:
        n = c.execute(text("SELECT count(*) FROM cn_fhir_bundle WHERE rv_item_id = :i"),
                      {"i": item}).scalar_one()  # fmt: skip
    assert n == 0  # nothing promoted


def test_unknown_item_and_missing_bundle(client):
    zero = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/reviewer/items/{zero}/elements").status_code == 404
    assert _post(client, f"/api/reviewer/items/{zero}/claim", reviewer="x").status_code == 404
    _, item = _item_with_held_facts(["Tab Metformin 500 mg BD"])
    assert client.get(f"/api/reviewer/items/{item}/bundle").status_code == 404


def test_admin_inspector_is_allow_listed(client):
    r = client.get("/admin")
    assert r.status_code == 200
    for t in ("cn_lab_result", "rv_item", "ai_event", "ops_patient_visit", "fin_billing_item"):
        assert f"/admin?table={t}" in r.text
    assert "table=clinical_fact" not in r.text and "table=patient_registry" not in r.text
    assert client.get("/admin", params={"table": "rv_item"}).status_code == 200
    for bad in ("clinical_fact", "patient_registry", "rv_item; DROP TABLE rv_item"):
        assert client.get("/admin", params={"table": bad}).status_code == 404
    assert client.get("/admin", params={"mpi": "CFP-0000-000000"}).status_code == 404


@pytest.mark.parametrize("path,marker", [("/reviewer", "Workbasket"), ("/admin", "Data inspector")])
def test_pages_render(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200 and marker in r.text and "cdn" not in r.text.lower()
