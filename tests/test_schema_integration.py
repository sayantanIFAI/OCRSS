"""E0-S2 (integration): migrations 0001–0008 against a real PostgreSQL 16, plus the core DAL."""

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
AUTH_TABLES = {"web_user", "web_session"}  # migration 0005 (backlog E11-S1)
MODEL_TABLES = {"model_version", "model_rollout"}  # migration 0006 (backlog E10-S5)
JOB_TABLES = {"web_job"}  # migration 0007 (backlog E10-S9)
# migration 0009: the medicine catalogue (Tata 1mg) and each medication's identification
CATALOGUE_TABLES = {"med_catalogue", "med_catalogue_import", "medication_catalogue_match"}
AGENT_TABLES = {"agent_trace"}  # migration 0010 (backlog E12-S5)
ORDER_TABLES = {"cn_lab_order"}  # migration 0011: tests a prescription advises
# migration 0013 (backlog E4-S7): ocr_block became ocr_observation; ocr_block is now a view
EVIDENCE_TABLES = {
    "ocr_observation",
    "interpretation_candidate",
    "verified_fact",
    "ocr_agreement",
}  # 0013 (E4-S7) + 0018 (E4-S11)
LONGITUDINAL_TABLES = {
    "medication_supersession",  # migration 0014 (backlog E5-S1)
    "problem_continuity",
}  # migration 0023 (E5-S1 problem list)
ORCHESTRATOR_TABLES = {"workflow_step", "workflow_replay", "agent_tool_call"}  # 0019 (E12-S1)
# migration 0021 (backlog E9-S1): a schema change with the cluster up
MIGRATION_TABLES = {"schema_gate", "migration_run"}
# migration 0022 (backlog E9-S3): what stopped being retried
QUEUE_TABLES = {"dead_letter"}
# migration 0026 (backlog E5-S2): the derived patient summary
SUMMARY_TABLES = {"patient_summary"}
TRAINING_TABLES = {
    "calibration_adjudication",  # 0025: durable clinician decisions
    "calibration_dataset",
    "calibration_training_example",  # 0015 (E4-S1)
    "calibration_model",
    "calibration_slice",  # 0016 (E4-S2)
    "gate_threshold_set",
    "gate_threshold",
}  # 0017 (E4-S3)
ALL_TABLES = ((PER_DOC_TABLES - {"ocr_block"}) | {"patient_registry"} | CANONICAL_TABLES
              | AUTH_TABLES | MODEL_TABLES | JOB_TABLES | CATALOGUE_TABLES | AGENT_TABLES
              | ORDER_TABLES | EVIDENCE_TABLES | LONGITUDINAL_TABLES
              | TRAINING_TABLES | ORCHESTRATOR_TABLES | MIGRATION_TABLES
              | QUEUE_TABLES | SUMMARY_TABLES)  # fmt: skip
VIEWS = {"v_problem_list", "v_medication_list", "v_allergy_list", "v_results_grid",
         "v_encounter_timeline", "ocr_block", "v_medication_history",
         "v_problem_history", "v_patient_summary"}  # fmt: skip


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
    assert _tables(engine) == ALL_TABLES
    assert _views(engine) == VIEWS
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0027"
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
        # an auto_accepted fact needs its evidence (migration 0012, backlog E4-S5)
        c.execute(
            text(
                "INSERT INTO fact_provenance (fact_id, extracted_text) SELECT id, local_text "
                "FROM clinical_fact WHERE patient_id = :p AND review_state = 'auto_accepted'"
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
    assert _tables(engine) == ALL_TABLES
