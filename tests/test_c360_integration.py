"""E7-S8: Customer 360 read model (real PostgreSQL).

``GET /api/reviewer/c360/{mpi}`` returns the patient's demographics, both identifiers (ABHA and
legacy MRN, side by side) and the canonical clinical facts across documents; ``/reviewer/c360``
renders it and every Reviewer row links to it (ARCHITECTURE §4.5, §7.1)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cdi_adapter.canon.repo import create_rv_item_for_document, customer_360
from cdi_adapter.db import session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    return signed_in(app, "reviewer")


def _approved_item(client, header: list[str], lines: list[str]) -> tuple[str, str]:
    doc, blocks = _doc_with_ocr(header + ["Rx"] + lines)
    out = _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        item = str(create_rv_item_for_document(c, doc, job_id="job-c360"))
    assert client.post(f"/api/reviewer/items/{item}/claim", json={}).status_code == 200
    assert client.post(f"/api/reviewer/items/{item}/approve", json={}).status_code == 200
    return doc, str(out["patient_id"])


def test_customer_360_reads_the_canonical_record_across_documents(seeded_scans, client):
    header = _rx()[:4]  # one patient, two prescriptions
    doc1, p1 = _approved_item(client, header, ["Tab Amlodipine 5 mg OD"])
    doc2, p2 = _approved_item(client, header, ["Tab Atorvastatin 10 mg HS"])
    assert p1 == p2
    abha, mrn = "91-" + uuid.uuid4().hex[:12], "MRN-" + uuid.uuid4().hex[:8]
    with session_scope() as c:
        c.execute(text("UPDATE patient_identity SET abha_number = :a, legacy_mrn = :m "
                       "WHERE id = :p"), {"a": abha, "m": mrn, "p": p1})  # fmt: skip
        mpi = c.execute(text("SELECT mpi_id FROM patient_identity WHERE id = :p"),
                        {"p": p1}).scalar_one()  # fmt: skip

    r = client.get(f"/api/reviewer/c360/{mpi}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["patient"]["mpi_id"] == mpi and d["patient"]["name"]
    ids = {i["type"]: i["value"] for i in d["identifiers"]}
    # dual identifiers: the ABHA number and the legacy MRN coexist, neither replaces the other
    assert ids["ABHA number"] == abha and ids["Legacy MRN"] == mrn and ids["MPI"] == mpi
    assert {str(x["id"]) for x in d["documents"]} >= {doc1, doc2}
    drugs = {(m["drug_text"], m["document_id"]) for m in d["medications"]}
    assert {("Amlodipine", doc1), ("Atorvastatin", doc2)} <= drugs  # across documents
    assert d["conditions"] and all(x["review_state"] for x in d["conditions"])
    assert len(d["bundles"]) >= 2 and d["bundles"][0]["artifact_type"] == "PrescriptionRecord"


def _promoted(c, doc: str, rows: list[tuple[str, str]], at: str = "now()") -> tuple[str, str]:
    """Canonical rows as one approval writes them: one rv_item per document, every cn_* row of
    that approval with the same recorded_at (PostgreSQL only - no bundle is built here)."""
    item = str(create_rv_item_for_document(c, doc, job_id="job-c360"))
    p = c.execute(text("SELECT patient_id FROM rv_item WHERE id = :i"), {"i": item}).scalar_one()
    for table, value in rows:
        col = {"cn_medication": "drug_text", "cn_condition": "display",
               "cn_lab_order": "test_text"}[table]  # fmt: skip
        c.execute(text(f"INSERT INTO {table} (patient_id, rv_item_id, review_state, recorded_by, "
                       f"recorded_at, {col}) VALUES (:p, :i, 'clinician_confirmed', 'dr-t', "
                       f"{at}, :v)"), {"p": p, "i": item, "v": value})  # fmt: skip
    return item, str(p)


def _prepared(lines: list[str]) -> str:
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    return doc


def test_the_read_model_joins_documents_identifiers_and_canonical_facts(db_env):
    header = _rx()[:4]
    doc1 = _prepared(header + ["Rx", "Tab Amlodipine 5 mg OD"])
    doc2 = _prepared(header + ["Rx", "Tab Atorvastatin 10 mg HS"])
    with session_scope() as c:
        _, p1 = _promoted(
            c, doc1, [("cn_medication", "Amlodipine"), ("cn_condition", "Hypertension")]
        )
        _, p2 = _promoted(
            c, doc2, [("cn_medication", "Atorvastatin"), ("cn_lab_order", "Lipid profile")]
        )
        assert p1 == p2  # one patient, two documents
        c.execute(text("UPDATE patient_identity SET abha_number = '91-7788-6655-4433', "
                       "legacy_mrn = 'MRN-C360' WHERE id = :p"), {"p": p1})  # fmt: skip
        mpi = c.execute(text("SELECT mpi_id FROM patient_identity WHERE id = :p"),
                        {"p": p1}).scalar_one()  # fmt: skip
        d = customer_360(c, mpi)
        assert customer_360(c, "CFP-0000-000000") is None
    assert d is not None
    assert [(i["type"], i["value"]) for i in d["identifiers"]] == [
        ("MPI", mpi),
        ("ABHA number", "91-7788-6655-4433"),
        ("Legacy MRN", "MRN-C360"),
    ]
    assert {str(x["id"]) for x in d["documents"]} == {doc1, doc2}
    assert {(m["drug_text"], str(m["document_id"])) for m in d["medications"]} == {
        ("Amlodipine", doc1),
        ("Atorvastatin", doc2),
    }
    assert [(x["display"], str(x["document_id"])) for x in d["conditions"]] == [
        ("Hypertension", doc1)
    ]
    assert [x["test_text"] for x in d["lab_orders"]] == ["Lipid profile"]
    assert all(m["review_state"] == "clinician_confirmed" and m["recorded_by"] == "dr-t"
               for m in d["medications"])  # fmt: skip


def test_a_reapproved_document_shows_its_latest_promotion_once(db_env):
    doc = _prepared(_rx()[:4] + ["Rx", "Tab Amlodipine 5 mg OD"])
    with session_scope() as c:
        item, p = _promoted(c, doc, [("cn_medication", "Amlodipine")], "now() - interval '1 hour'")
        # approved again after the document was refreshed: the same item, a later batch
        c.execute(text("INSERT INTO cn_medication (patient_id, rv_item_id, review_state, "
                       "recorded_by, recorded_at, drug_text) VALUES (:p, :i, 'corrected', 'dr-t', "
                       "now(), 'Amlodipine 10')"), {"p": p, "i": item})  # fmt: skip
        mpi = c.execute(text("SELECT mpi_id FROM patient_identity WHERE id = :p"),
                        {"p": p}).scalar_one()  # fmt: skip
        d = customer_360(c, mpi)
    assert d is not None
    assert [m["drug_text"] for m in d["medications"]] == ["Amlodipine 10"]


def test_customer_360_unknown_patient_and_page(client):
    assert client.get("/api/reviewer/c360/CFP-0000-000000").status_code == 404
    page = client.get("/reviewer/c360", params={"mpi": "CFP-0000-000000"})
    assert (
        page.status_code == 200 and "Customer 360" in page.text and "cdn" not in page.text.lower()
    )
    # linked from the Reviewer's rows and its open item
    assert "/reviewer/c360?mpi=" in client.get("/reviewer").text


def test_customer_360_needs_a_clinician_session(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import browser, signed_in

    for c, status in ((browser(app), 401), (signed_in(app, "intake"), 403)):
        assert c.get("/api/reviewer/c360/CFP-0000-000000").status_code == status
