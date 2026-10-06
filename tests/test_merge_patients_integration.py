"""scripts/merge_patients.py against real PostgreSQL: a duplicate patient merged into the right
record, an approval made twice removed with everything it wrote, nothing else touched."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from cdi_adapter.canon import promote
from cdi_adapter.canon.repo import create_rv_item_for_document
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import merge_patients  # noqa: E402


def _approved_doc() -> tuple[str, str, str]:
    """A prescription of a new patient (``_rx`` names one no other test shares), extracted,
    validated and approved."""
    doc, blocks = _doc_with_ocr(_rx()[:4] + ["Rx", "Tab Amlodipine 5 mg OD"])
    pid = _write(doc, blocks)["patient_id"]
    bind_document(doc)
    validate_document(doc)
    with session_scope() as c:
        item = str(create_rv_item_for_document(c, doc, job_id="job-m"))
        promote.claim(c, item, "rev")
        for f in c.execute(
            text(
                "SELECT id FROM clinical_fact WHERE is_current AND "
                "review_state IN ('pending','in_review') AND "
                "CAST(:d AS uuid) = ANY(source_doc_ids)"
            ),
            {"d": doc},
        ).scalars():
            c.execute(text("UPDATE clinical_fact SET review_state = 'clinician_confirmed' "
                           "WHERE id = :i"), {"i": f})  # fmt: skip
        promote.promote_review_item(c, item, "rev")
        mpi = c.execute(text("SELECT mpi_id FROM patient_identity WHERE id = :p"),
                        {"p": pid}).scalar_one()  # fmt: skip
    return doc, item, mpi


def _count(sql: str, **p: object) -> int:
    with session_scope() as c:
        return int(c.execute(text(sql), p).scalar_one())


def test_merge_moves_the_duplicates_records_and_removes_its_extra_approval(seeded_scans, db_env):
    doc1, item1, keep = _approved_doc()
    doc2, item2, drop = _approved_doc()
    assert _count("SELECT count(*) FROM cn_fhir_bundle WHERE rv_item_id = :i", i=item2) == 1

    with get_engine().connect() as c:  # the dry run changes nothing
        tx = c.begin()
        plan = merge_patients.merge(c, keep, drop, [item2])
        tx.rollback()
    assert plan["removed_approvals"][item2]["cn_fhir_bundle"] == 1
    assert _count("SELECT count(*) FROM patient_identity WHERE mpi_id = :m", m=drop) == 1

    assert merge_patients.main(["--keep", keep, "--drop", drop, "--remove-approval", item2,
                                "--apply"]) == 0  # fmt: skip
    keep_id = _count("SELECT count(*) FROM patient_identity WHERE mpi_id = :m", m=keep)
    assert keep_id == 1 and _count("SELECT count(*) FROM patient_identity WHERE mpi_id = :m",
                                   m=drop) == 0  # fmt: skip
    with session_scope() as c:
        pid = c.execute(text("SELECT id FROM patient_identity WHERE mpi_id = :m"),
                        {"m": keep}).scalar_one()  # fmt: skip
        doc2_patient = c.execute(text("SELECT patient_id FROM source_document WHERE id = :d"),
                                 {"d": doc2}).scalar_one()  # fmt: skip
        facts2 = set(c.execute(text("SELECT patient_id FROM clinical_fact WHERE "
                                    "CAST(:d AS uuid) = ANY(source_doc_ids)"), {"d": doc2}
                               ).scalars())  # fmt: skip
        audit = c.execute(text("SELECT detail FROM audit_log WHERE entity = 'patient_identity' "
                               "AND actor = 'merge_patients' ORDER BY at DESC LIMIT 1")
                          ).scalar_one()  # fmt: skip
    assert doc2_patient == pid and facts2 == {pid}  # the duplicate's document now the kept one's
    # the extra approval is gone with everything it wrote; the kept approval is untouched
    for sql in ("SELECT count(*) FROM rv_item WHERE id = :i",
                "SELECT count(*) FROM cn_fhir_bundle WHERE rv_item_id = :i",
                "SELECT count(*) FROM cn_medication WHERE rv_item_id = :i"):  # fmt: skip
        assert _count(sql, i=item2) == 0
        assert _count(sql, i=item1) >= 1
    assert audit["merged"] == drop and item2 in audit["removed_approvals"]


def test_merge_refuses_to_remove_what_is_not_an_approval(seeded_scans, db_env):
    _, _, keep = _approved_doc()
    _, _, drop = _approved_doc()
    lines = _rx()[:4] + ["Rx", "Tab Amlodipine 5 mg OD"]
    doc, blocks = _doc_with_ocr(lines)
    _write(doc, blocks)
    with session_scope() as c:
        open_item = str(create_rv_item_for_document(c, doc, job_id="job-o"))
    with pytest.raises(SystemExit, match="refused"):
        merge_patients.main(["--keep", keep, "--drop", drop, "--remove-approval", open_item,
                             "--apply"])  # fmt: skip
    assert _count("SELECT count(*) FROM patient_identity WHERE mpi_id = :m", m=drop) == 1
