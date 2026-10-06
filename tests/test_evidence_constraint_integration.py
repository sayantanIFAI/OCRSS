"""E4-S5: no evidence => cannot be auto_accepted, enforced by the database (real PostgreSQL).

AC: setting ``review_state = 'auto_accepted'`` on a ``clinical_fact`` with zero ``fact_provenance``
rows fails at the database level, shown by direct SQL that bypasses the application layer
(migration 0012, a deferred constraint trigger: checked at commit)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from cdi_adapter.db import get_engine, session_scope
from cdi_adapter.terminology.service import bind_document
from cdi_adapter.validate.service import validate_document
from tests.test_extract_integration import _doc_with_ocr, _write
from tests.test_validate_integration import _rx

pytestmark = pytest.mark.integration

REFUSED = "cannot be auto_accepted"


def _patient(c) -> str:
    return str(c.execute(text("INSERT INTO patient_identity (name) VALUES ('E4-S5 Test') "
                              "RETURNING id")).scalar_one())  # fmt: skip


def _fact(c, patient: str, state: str = "pending") -> str:
    return str(c.execute(
        text("INSERT INTO clinical_fact (patient_id, fact_type, local_text, review_state) "
             "VALUES (:p, 'condition', 'Hypertension', :s) RETURNING id"),
        {"p": patient, "s": state},
    ).scalar_one())  # fmt: skip


def _provenance(c, fact: str) -> None:
    c.execute(text("INSERT INTO fact_provenance (fact_id, extracted_text) VALUES (:f, 'HTN')"),
              {"f": fact})  # fmt: skip


def _state(fact: str) -> str | None:
    with get_engine().connect() as c:
        return c.execute(text("SELECT review_state FROM clinical_fact WHERE id = :f"),
                         {"f": fact}).scalar_one_or_none()  # fmt: skip


def test_direct_sql_cannot_auto_accept_a_fact_with_no_provenance(db_env):
    """The AC's negative test: plain SQL on the database, no application code involved."""
    with session_scope() as c:
        fact = _fact(c, _patient(c))
    with pytest.raises(IntegrityError, match=REFUSED):
        with get_engine().begin() as c:
            c.execute(text("UPDATE clinical_fact SET review_state = 'auto_accepted' "
                           "WHERE id = :f"), {"f": fact})  # fmt: skip
    assert _state(fact) == "pending"  # the transaction was rolled back


def test_direct_sql_cannot_insert_a_fact_already_auto_accepted(db_env):
    with session_scope() as c:
        patient = _patient(c)
    with pytest.raises(IntegrityError, match=REFUSED):
        with get_engine().begin() as c:
            _fact(c, patient, "auto_accepted")
    with get_engine().connect() as c:
        n = c.execute(text("SELECT count(*) FROM clinical_fact WHERE patient_id = :p"),
                      {"p": patient}).scalar_one()  # fmt: skip
    assert n == 0


def test_a_fact_with_evidence_may_be_auto_accepted(db_env):
    with get_engine().begin() as c:
        # fact first, its provenance later in the same transaction: the check runs at commit
        fact = _fact(c, _patient(c), "auto_accepted")
        _provenance(c, fact)
    assert _state(fact) == "auto_accepted"


def test_the_state_at_commit_counts_not_an_intermediate_one(db_env):
    with session_scope() as c:
        fact = _fact(c, _patient(c))
    with get_engine().begin() as c:  # auto_accepted, then held again before commit: allowed
        c.execute(text("UPDATE clinical_fact SET review_state = 'auto_accepted' WHERE id = :f"),
                  {"f": fact})  # fmt: skip
        c.execute(text("UPDATE clinical_fact SET review_state = 'in_review' WHERE id = :f"),
                  {"f": fact})  # fmt: skip
    assert _state(fact) == "in_review"


def test_other_states_need_no_provenance(db_env):
    with get_engine().begin() as c:
        p = _patient(c)
        facts = [_fact(c, p, s) for s in ("pending", "in_review", "rejected")]
    assert [_state(f) for f in facts] == ["pending", "in_review", "rejected"]


def test_the_pipeline_still_auto_accepts_facts_with_evidence(db_env):
    """No behaviour change: S4 writes provenance for every fact, the S6 gate auto-accepts."""
    doc, blocks = _doc_with_ocr(_rx())
    _write(doc, blocks)
    bind_document(doc)
    out = validate_document(doc)
    assert out["auto_accepted"] > 0
    with session_scope() as c:
        facts = repo.get_facts_for_document(c, doc)
        accepted = [f for f in facts if f["review_state"] == "auto_accepted"]
        assert accepted and all(repo.get_provenance(c, f["id"]) for f in accepted)
