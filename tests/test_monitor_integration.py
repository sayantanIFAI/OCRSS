"""The Pipeline Monitor microsite (``/monitor``, owner request 2026-09-26) on real PostgreSQL: a
document that went OCR -> extract -> terminology -> validate shows each stage with its details."""

from __future__ import annotations

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

    monkeypatch.setenv("CDI_S3_ENDPOINT_URL", "http://127.0.0.1:9")  # no object store needed
    get_settings.cache_clear()
    storage.get_store.cache_clear()
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    yield app, signed_in(app, "reviewer")
    get_settings.cache_clear()
    storage.get_store.cache_clear()


def test_a_processed_document_shows_every_stage_and_its_details(client):
    app, c = client
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)

    flow = c.get(f"/api/monitor/documents/{doc}").json()
    stages = {s["key"]: s for s in flow["stages"]}
    assert [s["key"] for s in flow["stages"]] == [
        "ingest",
        "ocr",
        "classify",
        "extract",
        "terminology",
        "validate",
        "agent",
        "review",
        "fhir",
    ]
    for key in ("terminology", "validate"):
        assert stages[key]["state"] == "ok" and stages[key]["run"]["seconds"] is not None
    assert stages["ocr"]["detail"]["blocks"] == len(blocks)
    assert [ln["text"] for ln in stages["ocr"]["detail"]["lines"]] == [b["text"] for b in blocks]
    terms = stages["terminology"]["detail"]
    assert terms["total"] == 7 and terms["coded"] >= 1
    assert {f["fact_type"] for f in terms["facts"]} >= {"medication", "condition"}
    assert stages["extract"]["detail"]["facts"] == 7
    assert stages["validate"]["detail"]["facts"] == 7
    assert stages["fhir"]["state"] in ("ready", "ok")  # validated: a bundle can be built
    assert flow["document"]["id"] == doc

    assert c.get("/api/monitor/items").status_code == 200
    assert c.get("/api/monitor/documents/00000000-0000-0000-0000-000000000000").status_code == 404


def test_the_monitor_is_a_microsite_for_clinicians_and_admins(client):
    from tests.helpers import signed_in

    app, reviewer = client
    page = reviewer.get("/monitor")
    assert page.status_code == 200 and "Pipeline Monitor" in page.text
    assert 'class="top"' not in page.text  # its own app, not a page under the staff header
    assert "/api/monitor/items" in page.text and "cdn" not in page.text.lower()
    # the staff site links to it (new tab)
    home = reviewer.get("/reviewer").text
    assert 'href="/monitor" target=_blank' in home
    counter = signed_in(app, "intake")  # the counter sees no clinical content
    assert counter.get("/monitor", follow_redirects=False).status_code in (303, 403)
    assert counter.get("/api/monitor/items").status_code == 403
