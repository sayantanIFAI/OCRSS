"""E0-S14: workbasket -> worklist -> promote -> canonical bundle, and /admin (real PostgreSQL).

E7-S1: every action is recorded under the signed-in user; a client-sent reviewer is ignored, and
only the reviewer who claimed an item (or an admin) may approve or reject it."""

from __future__ import annotations

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

    return signed_in(app, "reviewer")  # E11-S1: the clinician role owns /reviewer


@pytest.fixture
def other(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    return signed_in(app, "reviewer")  # a second clinician


def _me(c) -> str:
    return c.get("/api/me").json()["username"]


@pytest.fixture
def admin(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import signed_in

    return signed_in(app, "admin")  # E11-S1: /admin is admin-only


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


def test_a_document_uploaded_again_refreshes_its_item_and_tops_the_workbasket(client):
    # 2026-09-25: the same image uploaded again is the same document, extracted again; its old
    # facts are deleted (their elements with them) and the item was left empty at the bottom
    lines = _rx()[:4] + ["Rx", "Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"]
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        item = str(create_rv_item_for_document(c, doc, job_id="job-1"))
    _item_with_held_facts(["Tab Amlodipine 5 mg OD"])  # a newer upload, above it for now

    _write(doc, blocks)  # the upload again: re-extraction
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        assert c.execute(text("SELECT count(*) FROM rv_item_element WHERE rv_item_id = :i"),
                         {"i": item}).scalar_one() == 0  # fmt: skip
        assert str(create_rv_item_for_document(c, doc, job_id="job-2")) == item
        facts = c.execute(
            text(
                "SELECT count(*) FROM clinical_fact WHERE is_current AND "
                "CAST(:d AS uuid) = ANY(source_doc_ids)"
            ),
            {"d": doc},
        ).scalar_one()
        actions = c.execute(
            text(
                "SELECT action, detail->>'job_id' FROM rv_action WHERE rv_item_id = :i ORDER BY at"
            ),
            {"i": item},
        ).all()
        assert str(create_rv_item_for_document(c, doc, job_id="job-2")) == item  # no-op now
    assert [tuple(a) for a in actions] == [("create", "job-1"), ("refresh", "job-2")]
    wb = client.get("/api/reviewer/workbasket").json()
    assert wb[0]["id"] == item and wb[0]["job_id"] == "job-2"
    assert wb[0]["elements"] == facts == 4 and wb[0]["held"] == 1  # 2 dx, 2 meds


def test_full_workbasket_flow_promotes_and_builds_canonical_bundle(seeded_scans, client, other):
    doc, item = _item_with_held_facts(["Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD",
                                       "BP: 138/86 mmHg"])  # fmt: skip
    wb = {i["id"]: i for i in client.get("/api/reviewer/workbasket").json()}
    assert wb[item]["held"] == 1 and wb[item]["elements"] == 5  # 2 dx, 2 meds, 1 BP

    assert client.get(f"/api/reviewer/items/{item}/bundle/download").status_code == 404
    # an item is claimed into a worklist before it is decided (§4.5)
    assert _post(client, f"/api/reviewer/items/{item}/approve").status_code == 409
    assert _post(client, f"/api/reviewer/items/{item}/claim").status_code == 200
    assert item not in {i["id"] for i in client.get("/api/reviewer/workbasket").json()}
    worklist = client.get("/api/reviewer/worklist").json()
    assert [i["id"] for i in worklist] == [item]
    assert _post(other, f"/api/reviewer/items/{item}/claim").status_code == 409  # already mine

    # the reviewer works the held fact (correct it), then approves
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    held = next(e for e in els if e["state"] == "held")
    r = _post(client, f"/api/facts/{held['fact']['id']}/review", action="correct",
              corrections={"local_text": "Tab Metformin 500"})  # fmt: skip
    assert r.status_code == 200
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    corrected = next(e for e in els if e["fact_id"] == held["fact_id"])
    assert (
        corrected["state"] == "corrected" and corrected["fact"]["local_text"] == "Tab Metformin 500"
    )

    r = _post(client, f"/api/reviewer/items/{item}/approve")
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
    assert _post(client, f"/api/reviewer/items/{item}/approve").status_code == 409

    # the approved item leaves the worklist; its bundle is downloaded from "Approved" (2026-09-25)
    d = client.get(f"/api/reviewer/items/{item}/bundle/download")
    assert d.status_code == 200 and d.headers["content-type"].startswith("application/fhir+json")
    assert "attachment" in d.headers["content-disposition"]
    assert d.json() == b["bundle"] and d.json()["resourceType"] == "Bundle"  # FHIR, nothing else
    approved = client.get("/api/reviewer/approved").json()
    assert approved[0]["id"] == item and approved[0]["bundle_status"] == b["status"]


def test_approving_with_a_held_fact_never_promotes_it(seeded_scans, client):
    _, item = _item_with_held_facts(["Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"])
    _post(client, f"/api/reviewer/items/{item}/claim")
    r = _post(client, f"/api/reviewer/items/{item}/approve")  # §4.5: approve
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


def test_lab_item_promotes_into_cn_lab_result_test_code(seeded_scans, client, admin):
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
    _post(client, f"/api/reviewer/items/{item}/claim")
    r = _post(client, f"/api/reviewer/items/{item}/approve")
    assert r.json()["promoted"] == {"cn_lab_result": 2}
    page = admin.get("/admin", params={"mpi": mpi})  # patient_graph reads test_code (§4.5 fix)
    assert page.status_code == 200
    assert "4548-4" in page.text and "2160-0" in page.text and "DiagnosticReportRecord" in page.text


def test_reject_item(client):
    _, item = _item_with_held_facts(["Tab Metformin 5000 mg 1-1-1"])
    _post(client, f"/api/reviewer/items/{item}/claim")
    r = _post(client, f"/api/reviewer/items/{item}/reject", reason="wrong patient")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    with session_scope() as c:
        n = c.execute(text("SELECT count(*) FROM cn_fhir_bundle WHERE rv_item_id = :i"),
                      {"i": item}).scalar_one()  # fmt: skip
    assert n == 0  # nothing promoted


def test_unknown_item_and_missing_bundle(client):
    zero = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/reviewer/items/{zero}/elements").status_code == 404
    assert _post(client, f"/api/reviewer/items/{zero}/claim").status_code == 404
    _, item = _item_with_held_facts(["Tab Metformin 500 mg BD"])
    assert client.get(f"/api/reviewer/items/{item}/bundle").status_code == 404


def test_admin_inspector_is_allow_listed(admin):
    client = admin
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
def test_pages_render(admin, path, marker):
    client = admin  # admin may open every page
    r = client.get(path)
    assert r.status_code == 200 and marker in r.text and "cdn" not in r.text.lower()


# ------------------------------------------------------------------ E7-S1 (DoD-SEC misuse cases)


def test_a_spoofed_reviewer_is_ignored_on_claim_and_worklist(client, other):
    _, item = _item_with_held_facts(["Tab Metformin 500 mg BD"])
    r = _post(client, f"/api/reviewer/items/{item}/claim", reviewer=_me(other))
    assert r.status_code == 200 and r.json()["assigned_to"] == _me(client)  # not the spoofed one
    # a worklist is always the caller's own, whatever reviewer the query names
    assert item in [i["id"] for i in client.get("/api/reviewer/worklist").json()]
    theirs = other.get("/api/reviewer/worklist", params={"reviewer": _me(client)}).json()
    assert item not in [i["id"] for i in theirs]


def test_only_the_claiming_reviewer_may_decide_an_item(seeded_scans, client, other):
    _, item = _item_with_held_facts(["Tab Metformin 500 mg BD"])
    assert _post(client, f"/api/reviewer/items/{item}/claim").status_code == 200
    for action in ("approve", "reject"):
        r = _post(other, f"/api/reviewer/items/{item}/{action}", reviewer=_me(client))
        assert r.status_code == 403 and _me(client) in r.json()["detail"]
    with session_scope() as c:
        status = c.execute(text("SELECT status FROM rv_item WHERE id = :i"),
                           {"i": item}).scalar_one()  # fmt: skip
    assert status == "claimed"  # untouched
    assert _post(client, f"/api/reviewer/items/{item}/approve").status_code == 200


def test_an_admin_may_decide_a_claimed_item_under_their_own_name(client, admin):
    _, item = _item_with_held_facts(["Tab Metformin 500 mg BD"])
    _post(client, f"/api/reviewer/items/{item}/claim")
    r = _post(admin, f"/api/reviewer/items/{item}/reject", reviewer=_me(client), reason="dup")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    with session_scope() as c:
        rows = c.execute(text("SELECT actor, action FROM rv_action WHERE rv_item_id = :i "
                              "ORDER BY at"), {"i": item}).all()  # fmt: skip
    assert [tuple(r) for r in rows][-1] == (_me(admin), "reject")


def test_the_reviewer_console_needs_a_clinician_session(db_env):
    from cdi_adapter.webapp.app import app
    from tests.helpers import browser, signed_in

    _, item = _item_with_held_facts(["Tab Metformin 500 mg BD"])
    for c, status in ((browser(app), 401), (signed_in(app, "intake"), 403)):
        assert c.get("/api/reviewer/worklist").status_code == status
        for action in ("claim", "approve", "reject"):
            assert _post(c, f"/api/reviewer/items/{item}/{action}").status_code == status
    with session_scope() as c:
        assert c.execute(text("SELECT status FROM rv_item WHERE id = :i"),
                         {"i": item}).scalar_one() == "workbasket"  # fmt: skip


def test_a_decided_fact_stays_editable_until_its_item_is_approved(seeded_scans, client):
    # 2026-09-25 (owner): an Edit for every fact on the Reviewer page. A fact already accepted can
    # still be corrected, a medication's detail with it, until the item is approved
    _, item = _item_with_held_facts(["Tab Metformin 5000 mg 1-1-1", "Tab Amlodipine 5 mg OD"])
    _post(client, f"/api/reviewer/items/{item}/claim")
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    met = next(e for e in els if e["state"] == "held")
    fid = met["fact"]["id"]
    assert float(met["medication"]["strength_num"]) == 5000  # the page shows the medicine's detail
    assert _post(client, f"/api/facts/{fid}/review", action="accept").status_code == 200

    edit = {"drug_text": "Metformin", "strength_num": "500", "strength_unit": "mg",
            "frequency_code": "SOS", "duration_days": "5",
            "instructions": "after food"}  # fmt: skip
    r = _post(client, f"/api/facts/{fid}/review", action="correct", corrections=edit, note="dose")
    assert r.status_code == 200, r.text
    new_id = r.json()["result_fact_id"]
    with session_scope() as c:
        fact = c.execute(text("SELECT * FROM clinical_fact WHERE id = :i"),
                         {"i": new_id}).mappings().one()  # fmt: skip
        med = c.execute(text("SELECT * FROM medication_detail WHERE fact_id = :i"),
                        {"i": new_id}).mappings().one()  # fmt: skip
    assert fact["review_state"] == "corrected" and fact["local_text"] == "Metformin"
    assert (fact["code"], fact["code_status"], med["rxlike_code"]) == ("372567009", "bound",
                                                                      "372567009")  # fmt: skip
    assert (float(med["strength_num"]), med["strength_unit"], float(med["duration_days"])) == (
        500,
        "mg",
        5,
    )
    assert med["prn"] is True and med["frequency_per_day"] is None  # SOS: as needed
    assert med["instructions"] == "after food" and med["drug_text"] == "Metformin"
    # the element follows the new version; a corrected fact can be corrected again
    els = client.get(f"/api/reviewer/items/{item}/elements").json()["elements"]
    now = next(e for e in els if e["fact_id"] == fid)
    assert now["state"] == "corrected" and float(now["medication"]["strength_num"]) == 500
    bad = _post(client, f"/api/facts/{new_id}/review", action="correct",
                corrections={"strength_num": "five hundred"})  # fmt: skip
    assert bad.status_code == 422
    r = _post(client, f"/api/facts/{new_id}/review", action="correct",
              corrections={"frequency_code": "1-0-1"})  # fmt: skip
    assert r.status_code == 200
    newer = r.json()["result_fact_id"]

    assert _post(client, f"/api/reviewer/items/{item}/approve").status_code == 200
    with session_scope() as c:
        cn = c.execute(text("SELECT drug_text, strength_num, frequency_per_day FROM "
                            "cn_medication WHERE rv_item_id = :i AND drug_text = 'Metformin'"),
                       {"i": item}).mappings().one()  # fmt: skip
    assert float(cn["strength_num"]) == 500 and float(cn["frequency_per_day"]) == 2  # 1-0-1
    # approved: its values are in the canonical EMR, so no more edits here
    late = _post(client, f"/api/facts/{newer}/review", action="correct",
                 corrections={"instructions": "before food"})  # fmt: skip
    assert late.status_code == 409 and "approved" in late.json()["detail"]


def test_a_second_approve_waits_for_the_first_and_is_refused(seeded_scans, client):
    # 2026-09-25: approve runs the validator for seconds; clicks during that time each read the
    # item as still "claimed" and promoted it again (5 approvals, 5 copies in the canonical EMR)
    import threading

    from cdi_adapter.canon import promote
    from cdi_adapter.db import get_engine

    _, item = _item_with_held_facts(["Tab Amlodipine 5 mg OD"])
    _post(client, f"/api/reviewer/items/{item}/claim")
    me = _me(client)
    first = get_engine().connect()
    tx = first.begin()
    promote.promote_review_item(first, item, me)  # approved, not yet committed: row held
    second: dict[str, object] = {}

    def later() -> None:
        with get_engine().begin() as c:
            try:
                promote.promote_review_item(c, item, me)
                second["out"] = "approved again"
            except promote.WorkflowError as e:
                second["out"] = (e.status, e.detail)

    t = threading.Thread(target=later)
    t.start()
    t.join(timeout=2)
    assert t.is_alive()  # the second approval waits on the first's row lock
    tx.commit()
    first.close()
    t.join(timeout=30)
    assert second["out"] == (409, "item is approved")
    with session_scope() as c:
        n = c.execute(text("SELECT count(*) FROM cn_fhir_bundle WHERE rv_item_id = :i"),
                      {"i": item}).scalar_one()  # fmt: skip
        approvals = c.execute(text("SELECT count(*) FROM rv_action WHERE rv_item_id = :i "
                                   "AND action = 'approve'"), {"i": item}).scalar_one()  # fmt: skip
    assert n == 1 and approvals == 1


def test_approved_lists_a_document_once_its_latest_approval(seeded_scans, client):
    lines = _rx()[:4] + ["Rx", "Tab Amlodipine 5 mg OD"]
    doc, blocks = _doc_with_ocr(lines)
    items = []
    for _ in range(2):  # uploaded, approved; uploaded again (re-extracted), approved again
        _write(doc, blocks)
        bind_document(doc)
        validate_document(doc)
        with session_scope() as c:
            items.append(str(create_rv_item_for_document(c, doc, job_id="job-a")))
        _post(client, f"/api/reviewer/items/{items[-1]}/claim")
        assert _post(client, f"/api/reviewer/items/{items[-1]}/approve").status_code == 200
    assert items[0] != items[1]  # an approved item is never reopened: the new upload is new
    shown = [i["id"] for i in client.get("/api/reviewer/approved").json()]
    assert items[1] in shown and items[0] not in shown
