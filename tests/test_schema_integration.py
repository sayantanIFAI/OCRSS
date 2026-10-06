"""E0-S2 (integration): migrations 0001–0004 against a real PostgreSQL 16, plus the core DAL."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from cdi_adapter import repo
from tests.conftest import alembic_cfg

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
PER_DOC_TABLES = set(
    re.findall(r"^CREATE TABLE (\w+)", (ROOT / "db/schema.sql").read_text("utf-8"), re.M)
)
CANONICAL_TABLES = {
    "rv_item", "rv_item_element", "rv_action", "ai_event", "ai_suggestion",
    "ai_clinical_verification", "cn_condition", "cn_medication", "cn_lab_result",
    "cn_vital_sign", "cn_allergy", "cn_clinical_note", "cn_fhir_bundle",
    "ops_patient_visit", "fin_billing_item",
}  # fmt: skip
VIEWS = {"v_problem_list", "v_medication_list", "v_allergy_list", "v_results_grid",
         "v_encounter_timeline"}  # fmt: skip


def _tables(engine) -> set[str]:
    with engine.connect() as c:
        return set(
            c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars()
        ) - {"alembic_version"}


def _views(engine) -> set[str]:
    with engine.connect() as c:
        return set(
            c.execute(text("SELECT viewname FROM pg_views WHERE schemaname = 'public'")).scalars()
        )


@pytest.fixture(scope="module")
def engine(pg_url):
    command.upgrade(alembic_cfg(pg_url), "head")
    eng = create_engine(pg_url)
    yield eng
    eng.dispose()


def test_server_is_postgres_16(engine):
    with engine.connect() as c:
        assert c.execute(text("SHOW server_version_num")).scalar_one().startswith("16")


def test_upgrade_head_creates_every_table_and_view(engine):
    assert _tables(engine) == PER_DOC_TABLES | {"patient_registry"} | CANONICAL_TABLES
    assert _views(engine) == VIEWS
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0004"
        assert c.execute(text("SELECT nextval('patient_mpi_seq')")).scalar_one() >= 1


def test_registry_seeded_and_key_enforced(engine):
    with engine.begin() as c:
        seeds = c.execute(
            text("SELECT count(*) FROM patient_registry WHERE patient_id LIKE 'CFP-2025-9000%'")
        ).scalar_one()
        assert seeds == 4  # other tests may add registry rows to the shared test DB
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO patient_registry (patient_id, name, mobile, dob) "
                "VALUES ('CFP-X', 'RAMESH KUMAR', '9000000001', '1971-03-14')"
            )
        )  # same (mobile, lower(name), dob) as a seed row


def _patient(c) -> str:
    return str(
        c.execute(
            text("INSERT INTO patient_identity (name) VALUES ('Test Patient') RETURNING id")
        ).scalar_one()
    )


def test_check_constraints_reject_invalid_states(engine):
    for bad in ("review_state", "code_status", "fact_type"):
        with pytest.raises(IntegrityError), engine.begin() as c:
            pid = _patient(c)
            vals = {"review_state": "pending", "code_status": "unmapped", "fact_type": "condition"}
            vals[bad] = "bogus"
            c.execute(
                text(
                    "INSERT INTO clinical_fact (patient_id, fact_type, code_status, review_state) "
                    "VALUES (:p, :fact_type, :code_status, :review_state)"
                ),
                {"p": pid, **vals},
            )


def test_views_read_current_facts(engine):
    with engine.begin() as c:
        pid = _patient(c)
        c.execute(
            text(
                "INSERT INTO clinical_fact (patient_id, fact_type, local_text, review_state) "
                "VALUES (:p, 'condition', 'hypertension', 'auto_accepted'), "
                "       (:p, 'condition', 'old dx', 'rejected')"
            ),
            {"p": pid},
        )
        c.execute(text("UPDATE clinical_fact SET is_current = false WHERE local_text = 'old dx'"))
        rows = (
            c.execute(text("SELECT problem FROM v_problem_list WHERE patient_id = :p"), {"p": pid})
            .scalars()
            .all()
        )
    assert rows == ["hypertension"]


def test_repo_audit_run_and_status_round_trip(engine):
    with engine.begin() as c:
        doc_id = c.execute(
            text(
                "INSERT INTO source_document (sha256, filename, mime_type, byte_size, storage_key) "
                "VALUES (repeat('a', 64), 'x.pdf', 'application/pdf', 10, 'k') RETURNING id"
            )
        ).scalar_one()
        run = repo.start_run(c, document_id=doc_id, stage="ingest", model_name="pymupdf")
        repo.finish_run(c, run, metrics={"pages": 2})
        bad = repo.start_run(c, document_id=doc_id, stage="ocr")
        repo.fail_run(c, bad, error="boom")
        repo.set_document_status(c, doc_id, "pages_rendered")
        repo.set_document_status(c, doc_id, "error", error={"stage": "ocr", "message": "boom"})
        audit_id = repo.write_audit(
            c, actor="system", action="create", entity="source_document", entity_id=doc_id,
            detail={"filename": "x.pdf"},
        )  # fmt: skip

        runs = {r["stage"]: r for r in repo.get_runs(c, doc_id)}
        doc = repo.get_document(c, doc_id)
        audit = (
            c.execute(text("SELECT * FROM audit_log WHERE id = :i"), {"i": audit_id})
            .mappings()
            .one()
        )

    assert runs["ingest"]["status"] == "ok" and runs["ingest"]["metrics"] == {"pages": 2}
    assert runs["ingest"]["finished_at"] is not None
    assert runs["ocr"]["status"] == "failed" and runs["ocr"]["error"] == "boom"
    assert doc["status"] == "error" and doc["error"] == {"stage": "ocr", "message": "boom"}
    assert audit["entity_id"] == str(doc_id) and audit["detail"] == {"filename": "x.pdf"}


def test_downgrade_base_then_upgrade_round_trip(pg_url, engine):
    cfg = alembic_cfg(pg_url)
    command.downgrade(cfg, "base")
    assert _tables(engine) == set() and _views(engine) == set()
    command.upgrade(cfg, "head")
    assert _tables(engine) == PER_DOC_TABLES | {"patient_registry"} | CANONICAL_TABLES
