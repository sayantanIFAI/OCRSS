"""Tests a prescription advises (owner request 2026-09-28), on real PostgreSQL: an Rx with an
"Investigations:" line is extracted into one ``lab_order`` fact per test, coded, gated, shown in
the Reviewer, promoted into ``cn_lab_order`` and written into the approved record's narrative."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cdi_adapter import repo
from cdi_adapter.canon.promote import _promote_fact
from cdi_adapter.canon.repo import create_rv_item_for_document
from cdi_adapter.db import session_scope
from cdi_adapter.fhir.canonical import _advised_tests
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


def _rx_with_tests():
    lines = _rx()[:4] + ["Rx", "Tab Metformin 500 mg 1-0-1 x 30 days",
                         "Investigations: CBC, S. Creatinine, TFT, Urine R/M"]  # fmt: skip
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        item = str(create_rv_item_for_document(c, doc, job_id="job-orders"))
        orders = [f for f in repo.get_facts_for_document(c, doc) if f["fact_type"] == "lab_order"]
    return doc, item, orders


def test_advised_tests_are_extracted_coded_and_gated(db_env):
    _, _, orders = _rx_with_tests()
    by_text = {f["local_text"]: f for f in orders}
    assert sorted(by_text) == ["CBC", "S. Creatinine", "TFT", "Urine R/M"]
    assert (by_text["CBC"]["code_system"], by_text["CBC"]["code"]) == (
        "http://loinc.org",
        "58410-2",
    )
    assert by_text["S. Creatinine"]["code"] == "2160-0"
    assert by_text["Urine R/M"]["code"] == "24356-8"
    # no verified code: kept as written, never forced
    assert (by_text["TFT"]["code"], by_text["TFT"]["code_status"]) == (None, "local_only")
    # the S6 gate decides each one like any other fact, with its evidence line
    assert all(f["review_state"] in ("auto_accepted", "in_review") for f in orders)
    with session_scope() as c:
        assert all(repo.get_provenance(c, f["id"]) for f in orders)


def test_the_reviewer_shows_them_and_they_can_be_edited(client):
    _, item, orders = _rx_with_tests()
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    shown = [e for e in els if e["element_type"] == "lab_order"]
    assert sorted(e["fact"]["local_text"] for e in shown) == sorted(f["local_text"] for f in orders)
    tft = next(f for f in orders if f["local_text"] == "TFT")
    r = client.post(
        f"/api/facts/{tft['id']}/review",
        json={
            "action": "correct",
            "corrections": {"local_text": "Thyroid function test (T3, T4, TSH)"},
        },
    )
    assert r.status_code == 200, r.text
    with session_scope() as c:
        new = c.execute(text("SELECT fact_type, local_text, review_state FROM clinical_fact "
                             "WHERE id = :i"), {"i": r.json()["result_fact_id"]}).one()  # fmt: skip
    assert tuple(new) == ("lab_order", "Thyroid function test (T3, T4, TSH)", "corrected")


def test_approved_tests_go_into_the_emr_and_the_records_narrative(db_env):
    _, item, orders = _rx_with_tests()
    with session_scope() as c:
        for f in orders:  # what approval does for each governed fact of the item
            assert _promote_fact(c, f, item, "dr-test") == "cn_lab_order"
        rows = c.execute(
            text(
                "SELECT test_text, code, status, recorded_by FROM cn_lab_order "
                "WHERE rv_item_id = :i ORDER BY test_text"
            ),
            {"i": item},
        ).all()
        lines = _advised_tests(c, item)
    assert [tuple(r) for r in rows] == [
        ("CBC", "58410-2", "active", "dr-test"),
        ("S. Creatinine", "2160-0", "active", "dr-test"),
        ("TFT", None, "active", "dr-test"),
        ("Urine R/M", "24356-8", "active", "dr-test"),
    ]
    assert "Test advised: CBC (CBC panel - Blood by Automated count)" in lines
    assert "Test advised: TFT" in lines and len(lines) == 4


def test_a_prescription_without_tests_has_none(db_env):
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    with session_scope() as c:
        assert not [f for f in repo.get_facts_for_document(c, doc) if f["fact_type"] == "lab_order"]
