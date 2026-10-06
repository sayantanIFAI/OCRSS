"""Every detail on the Reviewer screen is editable (owner request 2026-09-28): besides the
medicine's name, strength, dose, frequency, duration, route and instructions, its form and
"as needed"; a lab's flag and reference range; a diagnosis' clinical status; and any fact's code.
All through the same correction as before (``POST /api/facts/{id}/review`` action ``correct``)."""

from __future__ import annotations

import uuid

import pytest
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
    from tests.helpers import signed_in

    return signed_in(app, "reviewer")


def _item(lines, doc_type=None):
    doc, blocks = _doc_with_ocr(lines, **({"doc_type": doc_type} if doc_type else {}))
    _write(doc, blocks, **({"doc_type": doc_type} if doc_type else {}))
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        return str(create_rv_item_for_document(c, doc))


def _els(client, item):
    return client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]


def _correct(client, fid, **corrections):
    return client.post(f"/api/facts/{fid}/review",
                       json={"action": "correct", "corrections": corrections})  # fmt: skip


def _row(table, col, fid):
    with session_scope() as c:
        return c.execute(text(f"SELECT * FROM {table} WHERE {col} = :i"),
                         {"i": fid}).mappings().one()  # fmt: skip


def test_a_medicines_form_as_needed_and_code_are_editable(client):
    item = _item(_rx()[:4] + ["Rx", "Tab Amlodipine 5 mg OD"])
    med = next(e for e in _els(client, item) if e["element_type"] == "medication")
    r = _correct(client, med["fact"]["id"], form="capsule", prn=True, code_system="http://snomed.info/sct",
                 code="386864001", code_display="Amlodipine")  # fmt: skip
    assert r.status_code == 200, r.text
    new_id = r.json()["result_fact_id"]
    fact, detail = _row("clinical_fact", "id", new_id), _row("medication_detail", "fact_id", new_id)
    assert (detail["form"], detail["prn"]) == ("capsule", True)
    assert (fact["code"], fact["code_display"], fact["code_status"]) == (
        "386864001", "Amlodipine", "bound")  # fmt: skip
    assert detail["frequency_code"] == "OD"  # the rest of the line is kept
    # the Reviewer shows the edited values
    now = next(e for e in _els(client, item) if e["element_type"] == "medication")
    assert now["state"] == "corrected" and now["medication"]["form"] == "capsule"


def test_a_labs_flag_and_reference_range_are_editable(client):
    name = "Editlab " + uuid.uuid4().hex[:6].translate(str.maketrans("0123456789", "ghijklmnop"))
    item = _item([f"Name: {name}  Age: 50 Y", "Date: 02/09/2026", "LABORATORY REPORT",
                  "HbA1c 7.8 % 4.0-5.6 H"], doc_type="lab_report")  # fmt: skip
    lab = next(e for e in _els(client, item) if e["element_type"] == "lab_result")
    fid = lab["fact"]["id"]
    r = _correct(client, fid, abnormal_flag="N", ref_range_low="-2", ref_range_high="6.5",
                 ref_range_text="-2 to 6.5")  # fmt: skip
    assert r.status_code == 200, r.text
    fact = _row("clinical_fact", "id", r.json()["result_fact_id"])
    assert fact["abnormal_flag"] == "N" and fact["ref_range_text"] == "-2 to 6.5"
    assert (float(fact["ref_range_low"]), float(fact["ref_range_high"])) == (-2.0, 6.5)
    # a blank clears the value; a word is refused
    r2 = _correct(client, r.json()["result_fact_id"], ref_range_low="")
    assert (
        r2.status_code == 200
        and _row("clinical_fact", "id", r2.json()["result_fact_id"])["ref_range_low"] is None
    )
    assert _correct(client, r2.json()["result_fact_id"], ref_range_high="high").status_code == 422


def test_a_diagnosis_clinical_status_is_editable(client):
    item = _item(_rx()[:4] + ["Rx", "Tab Amlodipine 5 mg OD"])
    dx = next((e for e in _els(client, item) if e["element_type"] == "condition"), None)
    if dx is None:
        pytest.skip("the worked-example prescription gave no diagnosis fact")
    r = _correct(client, dx["fact"]["id"], clinical_status="resolved")
    assert r.status_code == 200, r.text
    assert _row("clinical_fact", "id", r.json()["result_fact_id"])["clinical_status"] == "resolved"


def test_the_edit_form_offers_every_detail():
    from cdi_adapter.webapp.reviewer_page import SCRIPT

    for key in ("'form'", "'prn'", "'abnormal_flag'", "'ref_range_low'", "'ref_range_high'",
                "'ref_range_text'", "'clinical_status'", "'code_system'", "'code'",
                "'code_display'"):  # fmt: skip
        assert f"[{key}," in SCRIPT, key
