"""E0-S13 web app on real PostgreSQL (TestClient), plus the MinIO-dependent end-to-end job."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db_env, monkeypatch):
    from cdi_adapter import storage
    from cdi_adapter.config import get_settings

    monkeypatch.setenv("CDI_S3_ENDPOINT_URL", "http://127.0.0.1:9")  # nothing listens here
    get_settings.cache_clear()
    storage.get_store.cache_clear()
    from cdi_adapter.webapp.app import app

    yield TestClient(app)
    get_settings.cache_clear()
    storage.get_store.cache_clear()


def test_upload_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Intelligent OCR driven Agentic EMR generation" in r.text
    assert "type=file" in r.text and "name=sex" not in r.text  # identity is read, not typed
    assert "cdn" not in r.text.lower()


def test_healthz_reports_each_dependency(client):
    r = client.get("/healthz")
    body = r.json()
    assert body["db"] is True and body["mlserve"] is True  # stub backend
    assert body["s3"] is False and body["status"] == "degraded" and r.status_code == 503


@pytest.mark.parametrize("q", ["CFP-2025-900003", "9000000003"])
def test_registry_lookup(client, q):
    r = client.get("/api/registry/lookup", params={"q": q})
    assert r.status_code == 200 and r.json()["name"] == "Arjun Mehta"
    assert r.json()["dob"] == "1992-11-23"


def test_registry_lookup_miss(client):
    assert client.get("/api/registry/lookup", params={"q": "nobody"}).status_code == 404


def test_registry_persist_new_patient_then_found(client):
    doc, blocks = _doc_with_ocr(_rx())
    pid = _write(doc, blocks)["patient_id"]
    from cdi_adapter.db import session_scope
    from cdi_adapter.mpi.service import get_patient

    with session_scope() as c:
        mpi_id = get_patient(c, pid)["mpi_id"]
    mobile = "7" + str(uuid.uuid4().int)[:9]
    body = {"patient_id": mpi_id, "name": "New Reg", "mobile": mobile, "dob": "1980-02-03",
            "gender": "F", "address": "5 Test Rd"}  # fmt: skip
    r = client.post("/api/registry", json=body)
    assert r.status_code == 200 and r.json()["mobile"] == mobile
    assert client.get("/api/registry/lookup", params={"q": mobile}).json()["patient_id"] == mpi_id
    assert client.post("/api/registry", json={**body, "patient_id": "CFP-0000-999999"}
                       ).status_code == 404  # fmt: skip
    assert client.post("/api/registry", json={**body, "mobile": "abc"}).status_code == 422


def test_document_evidence_and_bundle_endpoints(client):
    doc, blocks = _doc_with_ocr(_rx())
    pid = _write(doc, blocks)["patient_id"]
    bind_document(doc)
    validate_document(doc)
    ev = client.get(f"/api/documents/{doc}/evidence").json()
    assert ev["doc_type"] == "prescription" and ev["ocr_blocks"] == len(blocks)
    assert len(ev["facts"]) == 7
    b = client.get(f"/api/documents/{doc}/bundle").json()
    assert b["status"] == "ready_to_share" and b["artifact_type"] == "PrescriptionRecord"
    pf = client.get(f"/api/patients/{pid}/fhir").json()
    assert pf["artifact_count"] >= 1
    assert client.get(f"/api/patients/{uuid.uuid4()}/fhir").status_code == 404
    assert client.get(f"/api/documents/{uuid.uuid4()}/evidence").status_code == 404


@pytest.mark.parametrize("n", [0, 11])
def test_job_upload_limits(client, n):
    files = [("files", (f"f{i}.pdf", b"%PDF-1.4", "application/pdf")) for i in range(n)]
    assert client.post("/api/jobs", files=files).status_code == 422


def test_job_with_unknown_patient_ref_is_404(client):
    files = [("files", ("a.pdf", b"%PDF-1.4", "application/pdf"))]
    r = client.post("/api/jobs", files=files, data={"patient_ref": "CFP-0000-000000"})
    assert r.status_code == 404


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/fhir").status_code == 404


def test_end_to_end_job_needs_minio(app_env):
    from cdi_adapter.webapp.app import app
    from cdi_adapter.webapp.jobs import get_runner
    from tests.helpers import make_pdf

    c = TestClient(app)
    pdf = make_pdf([_rx()])
    job_id = c.post("/api/jobs", files=[("files", ("rx.pdf", pdf, "application/pdf"))]).json()[
        "job_id"
    ]
    get_runner().wait(job_id)
    j = c.get(f"/api/jobs/{job_id}").json()
    assert j["state"] == "review" and j["documents"][0]["status"] == "done"
    assert c.get(f"/api/jobs/{job_id}/fhir").json()["artifact_count"] == 1
