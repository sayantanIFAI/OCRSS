"""The Reviewer's "Replace" for a medicine line (owner request 2026-09-28), on real PostgreSQL:
the options the catalogue and the medication agent offer, a catalogue search, and the replacement
itself (a reviewer's correction; the rest of the line is kept).

The test database is shared, so the catalogue loaded here is removed afterwards (as in
test_catalogue_integration)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.canon.repo import create_rv_item_for_document
from cdi_adapter.catalogue import service as cat
from cdi_adapter.db import session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).parent / "data" / "catalogue_sample_synthetic.csv"


@pytest.fixture
def catalogue(db_env):
    with session_scope() as c:
        cat.import_csv(c, SAMPLE, source="test-1mg", actor="pytest")
    yield
    with session_scope() as c:
        c.execute(text("DELETE FROM medication_catalogue_match"))
        c.execute(text("DELETE FROM med_catalogue"))
        c.execute(text("DELETE FROM med_catalogue_import"))


@pytest.fixture
def client(catalogue):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    return signed_in(app, "reviewer")


def _item(med_lines):
    doc, blocks = _doc_with_ocr(_rx()[:4] + ["Rx"] + med_lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        item = create_rv_item_for_document(c, doc, job_id="job-replace")
    return doc, str(item)


def _meds(doc):
    with session_scope() as c:
        return [f for f in repo.get_facts_for_document(c, doc)
                if f["fact_type"] == "medication" and f["is_current"]]  # fmt: skip


def _product_id(name):
    with session_scope() as c:
        return c.execute(text("SELECT id FROM med_catalogue WHERE name = :n"),
                         {"n": name}).scalar_one()  # fmt: skip


def test_a_medicine_is_replaced_with_a_catalogue_product_keeping_the_rest_of_the_line(client):
    doc, item = _item(["Tab Dola 650 mg 1-0-1"])  # a short look-alike brand: UNKNOWN here
    (fact,) = _meds(doc)
    d = client.get(f"/api/reviewer/facts/{fact['id']}/replacements").json()
    assert d["written"] and d["catalogue_empty"] is False
    assert all(o["why"] and o["name"] for o in d["catalogue"])
    found = client.get(f"/api/reviewer/facts/{fact['id']}/replacements", params={"q": "Dolo"})
    names = [o["name"] for o in found.json()["search"]]
    assert "Dolo 650 Tablet" in names

    dolo = _product_id("Dolo 650 Tablet")
    r = client.post(f"/api/reviewer/facts/{fact['id']}/replace",
                    json={"catalogue_id": dolo, "source": "search"})  # fmt: skip
    assert r.status_code == 200, r.text
    new_id = r.json()["result_fact_id"]
    assert new_id != str(fact["id"]) and r.json()["medicine"] == "Dolo 650 Tablet"
    (new,) = _meds(doc)
    assert str(new["id"]) == new_id and new["review_state"] == "corrected"
    with session_scope() as c:
        med = c.execute(text("SELECT drug_text, frequency_code FROM medication_detail "
                             "WHERE fact_id = :i"), {"i": new_id}).mappings().one()  # fmt: skip
        m = cat.matches_for_facts(c, [new_id])[new_id]
        action = c.execute(text("SELECT actor, detail FROM rv_action WHERE rv_item_id = :i "
                                "AND action = 'correct' ORDER BY at DESC LIMIT 1"),
                           {"i": item}).mappings().one()  # fmt: skip
    assert med["drug_text"] == "Dolo 650 Tablet" and med["frequency_code"] == "1-0-1"
    assert (m["status"], m["catalogue_id"], m["method"]) == ("matched", dolo, "reviewer")
    assert action["detail"]["replace_medication"] is True and action["detail"]["to"] == (
        "Dolo 650 Tablet"
    )
    assert action["actor"] == client.get("/api/me").json()["username"]
    # the Reviewer shows the chosen product for the line
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    shown = [e for e in els if e["element_type"] == "medication"]
    assert shown[0]["catalogue"]["method"] == "reviewer"
    assert shown[0]["medication"]["drug_text"] == "Dolo 650 Tablet"


def test_the_agents_product_and_options_are_offered(client):
    doc, _item_id = _item(["Tab Sonpraz 40 mg OD"])
    (fact,) = _meds(doc)
    sompraz, somprazd = _product_id("Sompraz 40 Tablet"), _product_id("Somprazd 40mg Capsule")
    suggestion = {
        "status": "suggested",
        "product": {"id": sompraz, "name": "Sompraz 40 Tablet"},
        "options": ["Somprazd 40mg Capsule", "Sompraz 40 Tablet"],
        "confidence": 0.8,
    }
    with session_scope() as c:
        event = c.execute(
            text(
                "INSERT INTO ai_event (source_document_id, kind) VALUES (:d, "
                "'medication_agent') RETURNING id"
            ),
            {"d": doc},
        ).scalar_one()
        c.execute(text("INSERT INTO ai_suggestion (ai_event_id, fact_id, element_type, suggested)"
                       " VALUES (:e, :f, 'medication', CAST(:s AS jsonb))"),
                  {"e": event, "f": str(fact["id"]), "s": json.dumps(suggestion)})  # fmt: skip
    d = client.get(f"/api/reviewer/facts/{fact['id']}/replacements").json()
    assert [(o["id"], o["why"]) for o in d["agent"]] == [
        (sompraz, "agent suggests"), (somprazd, "agent's options")]  # fmt: skip
    r = client.post(
        f"/api/reviewer/facts/{fact['id']}/replace",
        json={"catalogue_id": somprazd, "source": "agent", "note": "capsule on Rx"},
    )
    assert r.status_code == 200, r.text
    with session_scope() as c:
        m = cat.matches_for_facts(c, [r.json()["result_fact_id"]])[r.json()["result_fact_id"]]
    assert (m["catalogue_id"], m["method"]) == (somprazd, "reviewer")


def test_a_line_can_be_replaced_with_a_catalogue_generic(client):
    doc, _ = _item(["Tab Amlodipn 5 mg OD"])
    (fact,) = _meds(doc)
    r = client.post(f"/api/reviewer/facts/{fact['id']}/replace",
                    json={"generic": "Amlodipine", "source": "search"})  # fmt: skip
    assert r.status_code == 200, r.text
    new_id = r.json()["result_fact_id"]
    with session_scope() as c:
        m = cat.matches_for_facts(c, [new_id])[new_id]
    assert (m["status"], m["display_name"]) == ("matched", "Amlodipine (generic)")


def test_bad_replacements_are_refused_and_change_nothing(client):
    doc, _ = _item(["Tab Dolo 650 mg 1-0-1"])
    (fact,) = _meds(doc)
    url = f"/api/reviewer/facts/{fact['id']}/replace"
    assert client.post(url, json={"source": "search"}).status_code == 422  # nothing chosen
    assert client.post(url, json={"catalogue_id": 1, "generic": "Amlodipine"}).status_code == 422
    assert client.post(url, json={"catalogue_id": -5}).status_code == 404
    assert client.post(url, json={"generic": "Notadrug"}).status_code == 422
    assert client.post(url, json={"catalogue_id": 1, "source": "x"}).status_code == 422
    with session_scope() as c:
        other = c.execute(text("SELECT id FROM clinical_fact WHERE fact_type <> 'medication' AND "
                               "CAST(:d AS uuid) = ANY(source_doc_ids) LIMIT 1"),
                          {"d": doc}).scalar_one()  # fmt: skip
    assert client.get(f"/api/reviewer/facts/{other}/replacements").status_code == 422
    assert [f["id"] for f in _meds(doc)] == [fact["id"]]  # still the extracted fact


def test_replace_needs_a_clinician(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    c = signed_in(app, "intake")
    fid = "00000000-0000-0000-0000-000000000000"
    assert c.get(f"/api/reviewer/facts/{fid}/replacements").status_code == 403
    assert c.post(f"/api/reviewer/facts/{fid}/replace", json={"catalogue_id": 1}).status_code == 403


def test_a_document_uploaded_again_after_approval_is_correctable_again(client):
    """Owner's report 2026-09-28: the same screenshot was approved, then uploaded again; the new
    item's Replace (and Edit) failed with "its review item is approved", because the check looked
    for ANY approved item of the document. Only the item that holds the fact counts."""
    lines = _rx()[:4] + ["Rx", "Tab Dola 650 mg 1-0-1"]
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        first = create_rv_item_for_document(c, doc, job_id="job-first")
        # approved (the promotion itself is covered by test_reviewer_integration)
        c.execute(text("UPDATE rv_item SET status = 'approved' WHERE id = :i"), {"i": first})
    (old,) = _meds(doc)
    dolo = _product_id("Dolo 650 Tablet")
    url = f"/api/reviewer/facts/{old['id']}/replace"
    r = client.post(url, json={"catalogue_id": dolo, "source": "search"})
    assert r.status_code == 409 and "approved" in r.json()["detail"]  # its own item is approved

    _write(doc, blocks)  # the same file uploaded again: extracted again into new facts
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        second = create_rv_item_for_document(c, doc, job_id="job-second")
    assert second != first
    (new,) = _meds(doc)
    r = client.post(f"/api/reviewer/facts/{new['id']}/replace",
                    json={"catalogue_id": dolo, "source": "search"})  # fmt: skip
    assert r.status_code == 200, r.text
    fixed = r.json()["result_fact_id"]
    r = client.post(
        f"/api/facts/{fixed}/review",
        json={"action": "correct", "corrections": {"instructions": "after food"}},
    )
    assert r.status_code == 200, r.text  # Edit works on it too

    with session_scope() as c:  # once the new item is approved, it is locked in turn
        c.execute(text("UPDATE rv_item SET status = 'approved' WHERE id = :i"), {"i": second})
    late = client.post(
        f"/api/facts/{r.json()['result_fact_id']}/review",
        json={"action": "correct", "corrections": {"instructions": "x"}},
    )
    assert late.status_code == 409 and "approved" in late.json()["detail"]
