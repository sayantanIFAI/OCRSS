"""E0-S13 web app on real PostgreSQL (TestClient), plus the MinIO-dependent end-to-end job."""

from __future__ import annotations

import uuid

import pytest

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
    from tests.helpers import signed_in

    yield signed_in(app, "admin")  # E11-S1: every route needs a signed-in role
    get_settings.cache_clear()
    storage.get_store.cache_clear()


def test_upload_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Intelligent OCR driven Agentic EMR generation" in r.text
    assert "type=file" in r.text and "name=sex" not in r.text  # identity is read, not typed
    assert "cdn" not in r.text.lower()
    # the registry form lets staff type what the documents did not give (a name is required)
    assert "id=r_name" in r.text and "id=r_gender" in r.text and "p.age_text" in r.text


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


def test_document_evidence_and_bundle_endpoints(seeded_scans):
    # the bundle embeds the scan (§5 S9): the real MinIO, not the fixture's dead endpoint
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    client = signed_in(app, "admin")
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


def test_end_to_end_job_needs_minio(app_env, monkeypatch):
    from cdi_adapter.config import get_settings
    from cdi_adapter.webapp import jobstore

    # this process runs the job (a pod .env says queue, and no worker runs during the tests)
    monkeypatch.setenv("CDI_JOB_EXECUTION", "inprocess")
    get_settings.cache_clear()
    from cdi_adapter.webapp.app import app
    from tests.helpers import make_pdf, signed_in

    c = signed_in(app, "admin")
    pdf = make_pdf([_rx()])
    job_id = c.post("/api/jobs", files=[("files", ("rx.pdf", pdf, "application/pdf"))]).json()[
        "job_id"
    ]
    jobstore.wait(job_id)  # E10-S9: the job is in web_job, run by this instance
    j = c.get(f"/api/jobs/{job_id}").json()
    assert j["state"] == "review" and j["documents"][0]["status"] == "done"
    assert j["patient"]["age_text"] == "48 Y"  # as printed ("Age/Sex: 48 Y / F")
    assert c.get(f"/api/jobs/{job_id}/fhir").json()["artifact_count"] == 1
    # the evidence behind every fact, read back from MinIO: the page image and the scan as sent
    doc = j["documents"][0]["document_id"]
    facts = c.get(f"/api/jobs/{job_id}/facts").json()["documents"][0]["facts"]
    assert facts and all(f["page_image_url"] == f"/api/documents/{doc}/pages/1" for f in facts)
    page = c.get(f"/api/documents/{doc}/pages/1")
    assert page.status_code == 200 and page.headers["content-type"] == "image/png"
    assert page.content.startswith(b"\x89PNG")
    original = c.get(f"/api/documents/{doc}/original")
    assert original.status_code == 200 and original.content == pdf
    assert original.headers["content-type"] == "application/pdf"
    assert c.get(f"/api/documents/{doc}/pages/9").status_code == 404
    counter = signed_in(app, "intake")  # the counter hands over bundles, never the evidence
    assert counter.get(f"/api/documents/{doc}/original").status_code == 403
    # the download is FHIR itself: this upload's document Bundle, no wrapper
    dl = c.get(f"/api/jobs/{job_id}/fhir/download")
    assert dl.headers["content-type"].startswith("application/fhir+json")
    body = dl.json()
    assert body["resourceType"] == "Bundle" and body["type"] == "document"
    assert body["entry"][0]["resource"]["resourceType"] == "Composition"
    assert "bundles" not in body and "held_facts" not in body
