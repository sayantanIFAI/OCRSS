"""E0-S11 integration: S4 -> S5 -> S6 -> S9 on real PostgreSQL.

The test documents are recorded as > 700 KB, so the scan is also referenced by its s3:// URL (the
§7.2 large-scan path). Every bundle embeds the scan itself (§5 S9, attachment.data), so these tests
read a real MinIO (``seeded_scans``) and are skipped with the reason without one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cdi_adapter.db import session_scope
from cdi_adapter.fhir.service import bundle_hash, project_document, project_patient
from cdi_adapter.fhir.validate_abdm import validate
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration


@pytest.fixture
def url_scans(seeded_scans):
    yield


def _pipeline(lines, doc_type="prescription", **kw):
    doc, blocks = _doc_with_ocr(lines, doc_type=doc_type)
    out = _write(doc, blocks, doc_type=doc_type, **kw)
    bind_document(doc)
    validate_document(doc)
    return doc, out["patient_id"]


def _types(bundle):
    return [e["resource"]["resourceType"] for e in bundle["entry"]]


def test_fully_governed_prescription_is_ready_to_share(url_scans):
    doc, _ = _pipeline(_rx())
    out = project_document(doc)
    b = out["bundle"]
    assert (out["artifact_type"], out["status"], out["asserted_facts"]) == (
        "PrescriptionRecord", "ready_to_share", 7)  # fmt: skip
    assert out["held_facts"] == [] and [i for i in out["issues"] if i["severity"] == "error"] == []
    types = _types(b)
    assert types[0] == "Composition" and types.count("MedicationRequest") == 3
    assert types.count("Condition") == 2 and types.count("Observation") == 2  # BP + weight
    bp = next(
        e["resource"]
        for e in b["entry"]
        if e["resource"].get("resourceType") == "Observation"
        and e["resource"].get("code", {}).get("coding", [{}])[0].get("code") == "85354-9"
    )
    assert {
        c["code"]["coding"][0]["code"]: c["valueQuantity"]["value"] for c in bp["component"]
    } == {"8480-6": 138, "8462-4": 86}
    assert {c["code"]["coding"][0]["code"] for c in bp["component"]} == {"8480-6", "8462-4"}
    assert {"Patient", "Organization", "Practitioner", "Encounter", "Binary",
            "Provenance"} <= set(types)  # fmt: skip
    scan = next(e["resource"] for e in b["entry"] if e["resource"]["resourceType"] == "Binary")
    import base64

    assert base64.b64decode(scan["data"]).startswith(b"%PDF")
    assert scan["contentType"] == "application/pdf"
    assert validate(b, "PrescriptionRecord") == {"engine": "python", "valid": True, "issues": []}
    with session_scope() as c:
        row = c.execute(text("SELECT * FROM fhir_bundle WHERE source_document_id = :d"),
                        {"d": doc}).mappings().one()  # fmt: skip
        n_res = c.execute(text("SELECT count(*) FROM fhir_resource WHERE source_document_id=:d"),
                          {"d": doc}).scalar_one()  # fmt: skip
        status = c.execute(text("SELECT status FROM source_document WHERE id = :d"),
                           {"d": doc}).scalar_one()  # fmt: skip
    assert (row["status"], row["validation_status"], row["artifact_type"]) == (
        "ready_to_share", "valid", "PrescriptionRecord")  # fmt: skip
    assert row["bundle_hash"] == bundle_hash(row["bundle"]) and len(row["bundle_hash"]) == 64
    assert n_res == len(b["entry"]) and status == "projected"


def test_held_facts_are_reported_never_asserted(url_scans):
    lines = _rx()[:4] + ["Rx", "Tab Metformin 5000 mg 1-1-1"]  # dose ceiling -> held
    doc, _ = _pipeline(lines)
    out = project_document(doc)
    assert out["status"] == "draft" and len(out["held_facts"]) == 1
    assert out["held_facts"][0]["fact_type"] == "medication"
    assert "MedicationRequest" not in _types(out["bundle"])  # the gate is enforced at S9
    assert out["asserted_facts"] == 2  # the two governed conditions
    assert any(i["code"] == "held-fact" and i["severity"] == "info" for i in out["issues"])
    comp = out["bundle"]["entry"][0]["resource"]
    assert comp["status"] == "preliminary"


def test_accepting_the_held_fact_makes_it_shareable(url_scans):
    lines = _rx()[:4] + ["Rx", "Tab Metformin 5000 mg 1-1-1"]
    doc, _ = _pipeline(lines)
    held = project_document(doc)["held_facts"][0]["id"]
    with session_scope() as c:
        c.execute(text("UPDATE clinical_fact SET review_state = 'clinician_confirmed' "
                       "WHERE id = :i"), {"i": held})  # fmt: skip
    out = project_document(doc)
    assert out["status"] == "ready_to_share" and "MedicationRequest" in _types(out["bundle"])


def test_reprojection_keeps_stable_ids_and_replaces_rows(url_scans):
    doc, _ = _pipeline(_rx())
    a = project_document(doc)["bundle"]
    b = project_document(doc)["bundle"]
    assert [e["fullUrl"] for e in a["entry"]] == [e["fullUrl"] for e in b["entry"]]
    with session_scope() as c:
        assert c.execute(text("SELECT count(*) FROM fhir_bundle WHERE source_document_id = :d"),
                         {"d": doc}).scalar_one() == 1  # fmt: skip


def test_non_persisting_projection_writes_nothing(url_scans):
    doc, _ = _pipeline(_rx())
    out = project_document(doc, persist=False)
    assert out["status"] == "ready_to_share"
    with session_scope() as c:
        assert c.execute(text("SELECT count(*) FROM fhir_bundle WHERE source_document_id = :d"),
                         {"d": doc}).scalar_one() == 0  # fmt: skip


def test_lab_report_bundle_groups_observations_in_a_diagnostic_report(url_scans):
    lines = ["Name: Labcase Xq  Age: 48 Y", "Date: 11/08/2026", "LABORATORY REPORT",
             "HbA1c 7.8 % 4.0-5.6 H", "Fasting Blood Glucose 142 mg/dL 70-100 H",
             "Serum Creatinine 1.1 mg/dL 0.6-1.3"]  # fmt: skip
    doc, pid = _pipeline(lines, doc_type="lab_report")
    out = project_document(doc)
    assert out["artifact_type"] == "DiagnosticReportRecord" and out["status"] == "ready_to_share"
    dr = next(e["resource"] for e in out["bundle"]["entry"]
              if e["resource"]["resourceType"] == "DiagnosticReport")  # fmt: skip
    assert len(dr["result"]) == 3
    assert validate(out["bundle"], "DiagnosticReportRecord")["valid"]
    pp = project_patient(pid)
    assert pp["artifact_count"] >= 1 and pp["patient"]["name"]


def test_inline_scan_needs_minio(app_env):
    from cdi_adapter.ingest.service import ingest_bytes
    from tests.helpers import make_pdf

    res = ingest_bytes(make_pdf([["tiny"]]), "tiny.pdf")
    out = project_document(res.document_id, persist=False)
    docref = next(e["resource"] for e in out["bundle"]["entry"]
                  if e["resource"]["resourceType"] == "DocumentReference")  # fmt: skip
    assert "data" in docref["content"][0]["attachment"]
