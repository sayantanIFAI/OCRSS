"""The job's FHIR download (2026-09-25): proper FHIR, and only the documents of that upload."""

from __future__ import annotations

from typing import Any

from cdi_adapter.webapp.app import download_bundle, job_bundles


def _doc_bundle(n: int) -> dict[str, Any]:
    entry = {"fullUrl": f"urn:uuid:c{n}", "resource": {"resourceType": "Composition"}}
    return {"resourceType": "Bundle", "id": f"00000000-0000-4000-8000-00000000000{n}",
            "type": "document", "entry": [entry]}  # fmt: skip


def _item(n: int) -> dict[str, Any]:
    return {"document_id": f"doc-{n}", "filename": f"rx{n}.png", "doc_type": "prescription",
            "artifact_type": "PrescriptionRecord", "status": "draft", "issues": [],
            "held_facts": [{"id": "f1"}], "asserted_facts": 0,
            "bundle": _doc_bundle(n)}  # fmt: skip


PROJECTION = {"patient": {"id": "p1"}, "artifact_count": 3,
              "bundles": [_item(1), _item(2), _item(3)]}  # fmt: skip


def test_a_job_shows_only_the_documents_it_uploaded() -> None:
    out = job_bundles(PROJECTION, ["doc-2"])
    assert out["artifact_count"] == 1 and [b["document_id"] for b in out["bundles"]] == ["doc-2"]
    assert out["patient"] == {"id": "p1"}
    assert job_bundles(PROJECTION, [])["bundles"] == []


def test_one_document_downloads_as_its_own_fhir_document_bundle() -> None:
    body = download_bundle([_item(1)], "a" * 32, "2026-09-25T10:00:00+00:00")
    assert body == _doc_bundle(1)  # the FHIR Bundle itself: no wrapper, no review metadata
    assert "held_facts" not in body and "status" not in body


def test_several_documents_download_as_one_fhir_collection_bundle() -> None:
    body = download_bundle([_item(1), _item(2)], "a" * 32, "2026-09-25T10:00:00+00:00")
    assert body["resourceType"] == "Bundle" and body["type"] == "collection"
    assert body["timestamp"] == "2026-09-25T10:00:00+00:00"
    assert [e["resource"] for e in body["entry"]] == [_doc_bundle(1), _doc_bundle(2)]
    assert body["entry"][0]["fullUrl"] == "urn:uuid:00000000-0000-4000-8000-000000000001"
    # the same job always gets the same collection id
    assert body["id"] == download_bundle([_item(1)] * 2, "a" * 32, "x")["id"]


def test_the_upload_page_says_when_a_document_was_not_processed():
    # 2026-09-25: an upload read as "other" was skipped (0 facts) and the page still showed
    # "All facts governed"; nothing reached Review / Reviewer and nothing said why
    from cdi_adapter.webapp.page import SCRIPT

    assert "d.status === 'skipped'" in SCRIPT and "not processed" in SCRIPT
    assert "No facts extracted" in SCRIPT
    assert "facts ? '<span class=\"pill ok\">All facts governed</span>'" in SCRIPT
