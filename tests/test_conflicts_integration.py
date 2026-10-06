"""E5-S3 on real PostgreSQL: the merge console settles a conflict as one auditable decision.

The conflict is a real one - two lab reports of the same patient on the same day disagreeing about
HbA1c, which is what the S6 gate already writes as ``value_mismatch / CONTRADICTED / blocker`` with
both facts held (the same setup as ``test_cross_document_contradiction_and_duplicate``).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cdi_adapter.db import session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from cdi_adapter.webapp import conflicts
from cdi_adapter.webapp.conflicts import (
    ConflictResolution,
    conflict_detail,
    open_conflicts,
    resolve,
)
from cdi_adapter.webapp.review import ReviewError
from tests.test_extract_integration import _doc_with_ocr, _write

pytestmark = pytest.mark.integration

HBA1C = "4548-4"


def _unique(stem: str) -> str:
    """A name no other test shares: the MPI matches on name + age + sex, so two tests using
    look-alike names would pool their conflicts onto one patient."""
    tail = uuid.uuid4().hex[:8].translate(str.maketrans("0123456789", "ghijklmnop"))
    return f"{stem}{tail} Casey"


def _conflicting_pair(stem: str) -> tuple[str, str]:
    """One patient, two same-day lab reports that disagree: returns (conflict id, patient id)."""
    name = _unique(stem)
    lines_a = [f"Name: {name}  Age/Sex: 60 Y / M", "Date: 01/09/2026", "LABORATORY REPORT",
               "HbA1c 7.8 % 4.0-5.6", "Serum Creatinine 1.1 mg/dL"]  # fmt: skip
    lines_b = [ln.replace("HbA1c 7.8", "HbA1c 9.1") for ln in lines_a]
    doc_a, blocks_a = _doc_with_ocr(lines_a, doc_type="lab_report")
    pid = str(_write(doc_a, blocks_a, doc_type="lab_report")["patient_id"])
    bind_document(doc_a)
    validate_document(doc_a)
    doc_b, blocks_b = _doc_with_ocr(lines_b, doc_type="lab_report")
    _write(doc_b, blocks_b, doc_type="lab_report", patient_id=pid)
    bind_document(doc_b)
    validate_document(doc_b)
    with session_scope() as c:
        cid = c.execute(
            text("SELECT id FROM fact_conflict WHERE patient_id = :p "
                 "AND conflict_type = 'value_mismatch' AND NOT resolved"), {"p": pid},
        ).scalar_one()  # fmt: skip
    return str(cid), pid


def _state(fact_id: str) -> tuple[str, bool]:
    with session_scope() as c:
        r = c.execute(text("SELECT review_state, is_current FROM clinical_fact WHERE id = :i"),
                      {"i": fact_id}).first()  # fmt: skip
    return r[0], r[1]


def _row(cid: str) -> dict:
    with session_scope() as c:
        return dict(c.execute(text("SELECT * FROM fact_conflict WHERE id = :i"),
                              {"i": cid}).mappings().one())  # fmt: skip


def _audit(cid: str) -> list[dict]:
    with session_scope() as c:
        return [dict(r) for r in c.execute(
            text("SELECT actor, action, detail FROM audit_log WHERE entity = 'fact_conflict' "
                 "AND entity_id = :i ORDER BY id"), {"i": cid}).mappings()]  # fmt: skip


# ------------------------------------------------------------------ what the reviewer sees


def test_an_open_conflict_is_listed_with_both_sides(db_env):
    cid, pid = _conflicting_pair("Listed")
    with session_scope() as c:
        rows = open_conflicts(c)
    mine = [r for r in rows if str(r["id"]) == cid]
    assert len(mine) == 1
    r = mine[0]
    assert r["severity"] == "blocker" and r["conflict_type"] == "value_mismatch"
    assert r["evidence_state"] == "CONTRADICTED"
    assert {r["a"]["code"], r["b"]["code"]} == {HBA1C}
    assert {float(r["a"]["value_num"]), float(r["b"]["value_num"])} == {7.8, 9.1}
    assert r["a"]["review_state"] == r["b"]["review_state"] == "in_review"
    assert r["patient_name"]  # the queue says whose record is held up


def test_the_detail_puts_both_facts_side_by_side_with_their_own_evidence(db_env):
    """The acceptance's first half: each side carries its *own* evidence, not a shared blob."""
    cid, _ = _conflicting_pair("Sided")
    with session_scope() as c:
        d = conflict_detail(c, cid)
    assert set(d) == {"conflict", "a", "b"}
    for side in (d["a"], d["b"]):
        assert side["fact"]["code"] == HBA1C
        assert side["provenance"], "a side with no provenance cannot be judged"
        p = side["provenance"][0]
        assert p["extracted_text"] and p["bbox_union"]  # the OCR span and where it is on the page
        assert p["page_image_url"]
        assert "findings" in side and "resolution" in side
    assert d["a"]["fact"]["id"] != d["b"]["fact"]["id"]
    assert float(d["a"]["fact"]["value_num"]) != float(d["b"]["fact"]["value_num"])


# ------------------------------------------------------------------ keep A / keep B


def test_keep_a_confirms_one_fact_rejects_the_other_in_one_decision(db_env):
    cid, pid = _conflicting_pair("Kepta")
    row = _row(cid)
    a, b = str(row["fact_a"]), str(row["fact_b"])

    with session_scope() as c:
        out = resolve(c, cid, ConflictResolution(choice="keep_a", note="matches the page"),
                      reviewer="dr-anita")  # fmt: skip

    assert out["resolution"] == "keep_a"
    assert out["kept_fact_id"] == a and out["rejected_fact_id"] == b
    assert _state(a) == ("clinician_confirmed", True)
    assert _state(b) == ("rejected", False)
    after = _row(cid)
    assert after["resolved"] is True and after["resolution"] == "keep_a"
    assert after["resolved_by"] == "dr-anita" and after["resolved_at"] is not None
    assert after["note"] == "matches the page" and after["result_fact_id"] is None
    log = _audit(cid)
    assert len(log) == 1, "one decision, one audit row"  # the acceptance's second half
    assert log[0]["actor"] == "dr-anita" and log[0]["detail"]["resolution"] == "keep_a"
    assert log[0]["detail"]["kept_fact"] == a and log[0]["detail"]["rejected_fact"] == b


def test_keep_b_is_the_mirror(db_env):
    cid, _ = _conflicting_pair("Keptb")
    row = _row(cid)
    a, b = str(row["fact_a"]), str(row["fact_b"])
    with session_scope() as c:
        out = resolve(c, cid, ConflictResolution(choice="keep_b"), reviewer="dr-anita")
    assert out["kept_fact_id"] == b and out["rejected_fact_id"] == a
    assert _state(b) == ("clinician_confirmed", True)
    assert _state(a) == ("rejected", False)


# ------------------------------------------------------------------ merge


def test_merge_writes_a_corrected_fact_that_supersedes_the_base(db_env):
    """Merge is a correction of the fact you keep: a new current fact supersedes it, and the other
    side is rejected - one decision, with the merged value on the record."""
    cid, pid = _conflicting_pair("Merged")
    row = _row(cid)
    a, b = str(row["fact_a"]), str(row["fact_b"])

    with session_scope() as c:
        out = resolve(c, cid, ConflictResolution(choice="merge", base="a",
                                                 corrections={"value_num": 8.4},
                                                 note="repeat sample averaged"),
                      reviewer="dr-anita")  # fmt: skip

    result = out["result_fact_id"]
    assert result not in (a, b)
    with session_scope() as c:
        r = c.execute(text("SELECT value_num, review_state, is_current, supersedes "
                           "FROM clinical_fact WHERE id = :i"),
                      {"i": result}).mappings().one()  # fmt: skip
    assert float(r["value_num"]) == 8.4
    assert r["review_state"] == "corrected" and r["is_current"] is True
    assert str(r["supersedes"]) == a  # it supersedes the base, as an S8 correction does
    assert _state(b) == ("rejected", False)
    after = _row(cid)
    assert after["resolution"] == "merge" and str(after["result_fact_id"]) == result
    assert len(_audit(cid)) == 1


def test_merge_can_take_the_other_fact_as_its_base(db_env):
    cid, _ = _conflicting_pair("Mergedb")
    row = _row(cid)
    a, b = str(row["fact_a"]), str(row["fact_b"])
    with session_scope() as c:
        out = resolve(c, cid, ConflictResolution(choice="merge", base="b",
                                                 corrections={"value_num": 8.0}),
                      reviewer="dr-anita")  # fmt: skip
    with session_scope() as c:
        sup = c.execute(text("SELECT supersedes FROM clinical_fact WHERE id = :i"),
                        {"i": out["result_fact_id"]}).scalar_one()  # fmt: skip
    assert str(sup) == b
    assert _state(a) == ("rejected", False)


def test_merge_without_values_is_refused(db_env):
    """With nothing to merge the decision is keep_a, and saying so beats writing a 'merge' that
    changed nothing."""
    cid, _ = _conflicting_pair("Emptym")
    with session_scope() as c, pytest.raises(ReviewError, match="merge needs"):
        resolve(c, cid, ConflictResolution(choice="merge"), reviewer="dr-anita")
    assert _row(cid)["resolved"] is False  # and nothing was written


# ------------------------------------------------------------------ guards and the aftermath


def test_a_settled_conflict_cannot_be_settled_again(db_env):
    cid, _ = _conflicting_pair("Twice")
    with session_scope() as c:
        resolve(c, cid, ConflictResolution(choice="keep_a"), reviewer="dr-anita")
    with session_scope() as c, pytest.raises(ReviewError, match="already resolved"):
        resolve(c, cid, ConflictResolution(choice="keep_b"), reviewer="dr-bose")
    assert _row(cid)["resolved_by"] == "dr-anita"  # the first decision stands


def test_a_missing_conflict_is_a_404(db_env):
    with session_scope() as c, pytest.raises(ReviewError, match="not found"):
        resolve(c, str(uuid.uuid4()), ConflictResolution(choice="keep_a"), reviewer="dr-anita")


def test_settling_a_conflict_leaves_the_summary_current(db_env):
    """E5-S2's invariant holds through this path too: the decision changed governed facts, so the
    summary was rebuilt in the same transaction and nothing is left stale."""
    from cdi_adapter import summary

    cid, pid = _conflicting_pair("Summed")
    with session_scope() as c:
        resolve(c, cid, ConflictResolution(choice="keep_a"), reviewer="dr-anita")
    with session_scope() as c:
        assert summary.stale_patients(c) == []
        s = summary.patient_summary(c, pid)
    assert s is not None and s["stale"] is False


def test_the_open_queue_drops_a_settled_conflict(db_env):
    cid, _ = _conflicting_pair("Queued")
    with session_scope() as c:
        assert any(str(r["id"]) == cid for r in open_conflicts(c))
        resolve(c, cid, ConflictResolution(choice="keep_a"), reviewer="dr-anita")
        assert not any(str(r["id"]) == cid for r in open_conflicts(c))


def test_the_routes_and_the_page_are_wired(db_env):
    from cdi_adapter.webapp.app import app

    paths = set(app.openapi()["paths"])  # what a client can actually reach
    assert {"/api/conflicts", "/api/conflicts/{conflict_id}",
            "/api/conflicts/{conflict_id}/resolve", "/conflicts"} <= paths  # fmt: skip
    assert conflicts.CHOICES == ("keep_a", "keep_b", "merge")
