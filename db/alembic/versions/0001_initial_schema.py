"""Initial per-document schema (ARCHITECTURE §4.1–§4.4): db/schema.sql + db/views.sql.

Revision ID: 0001
Revises:
Create Date: 2026-09-21
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DB_DIR = Path(__file__).resolve().parents[2]  # .../db

VIEWS = (
    "v_encounter_timeline",
    "v_results_grid",
    "v_allergy_list",
    "v_medication_list",
    "v_problem_list",
)

# reverse dependency order
TABLES = (
    "audit_log",
    "abdm_transfer",
    "abdm_consent",
    "abdm_care_context",
    "review_task",
    "fhir_bundle",
    "fhir_resource",
    "fact_conflict",
    "fact_provenance",
    "medication_detail",
    "clinical_fact",
    "encounter",
    "patient_identity_alias",
    "extraction",
    "ocr_block",
    "doc_classification",
    "pipeline_run",
    "document_page",
    "source_document",
    "patient_identity",
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql((DB_DIR / "schema.sql").read_text(encoding="utf-8"))
    bind.exec_driver_sql((DB_DIR / "views.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    for v in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {v}")
    for t in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
