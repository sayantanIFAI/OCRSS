"""Clinician-adjudicated training data for calibration (backlog E4-S1, ARCHITECTURE §4.2-§4.3).

A versioned, append-only training table built from the S8 decisions already captured in
``clinical_fact`` (``review_state``, the ``supersedes`` correction chain) and ``fact_provenance``
(the reviewer-signed rows). ``cdi_adapter.training`` exports it on a schedule (nightly cron).

* ``calibration_dataset``: one row per exported version (1, 2, 3, ...) with the schema version,
  the SHA-256 of its content, its row count and when / by whom it was built. A new version is
  written only when the content changed.
* ``calibration_training_example``: one row per adjudicated fact in a version - the
  {proposed value, corrected value, doc_type, fact_type, confidence} triple plus the decision
  (confirmed / corrected / rejected), a 0/1 label (was the machine's value right), the component
  confidences, the machine state before the decision, the reviewer, the decision time and the
  model stack. No patient identifier is copied. ``proposed_fact_id`` / ``final_fact_id`` are
  plain pointers (no foreign key): a later re-extraction must not change a published version.

Both tables are append-only in the database: a published version is never updated or deleted.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-30
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_E = "integrity_constraint_violation"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE calibration_dataset (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            version         integer NOT NULL UNIQUE CHECK (version >= 1),
            schema_version  text NOT NULL,
            content_sha256  char(64) NOT NULL,
            row_count       integer NOT NULL CHECK (row_count >= 0),
            source          jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_by      text NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE calibration_training_example (
            id                      bigserial PRIMARY KEY,
            dataset_id              uuid NOT NULL REFERENCES calibration_dataset (id),
            proposed_fact_id        uuid NOT NULL,
            final_fact_id           uuid NOT NULL,
            document_id             uuid,
            doc_type                text,
            fact_type               text NOT NULL,
            decision                text NOT NULL CHECK (decision IN
                                        ('confirmed', 'corrected', 'rejected')),
            proposed                jsonb NOT NULL,
            corrected               jsonb,
            confidence              real CHECK (confidence BETWEEN 0 AND 1),
            confidence_extract      real,
            confidence_ocr          real,
            confidence_terminology  real,
            machine_state           text NOT NULL
                                        CHECK (machine_state IN ('auto_accepted', 'held')),
            label                   smallint NOT NULL CHECK (label IN (0, 1)),
            reviewer                text,
            decided_at              timestamptz,
            model_stack             jsonb,
            UNIQUE (dataset_id, proposed_fact_id),
            CHECK ((decision = 'rejected') = (corrected IS NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_calibration_example_slice "
        "ON calibration_training_example (dataset_id, doc_type, fact_type)"
    )
    op.execute(
        f"""
        CREATE FUNCTION calibration_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is a published training dataset: it is never updated or deleted '
                            '(backlog E4-S1)', TG_TABLE_NAME USING ERRCODE = '{_E}';
        END $$
        """
    )
    for table in ("calibration_dataset", "calibration_training_example"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION calibration_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION calibration_append_only()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calibration_training_example")
    op.execute("DROP TABLE IF EXISTS calibration_dataset")
    op.execute("DROP FUNCTION IF EXISTS calibration_append_only()")
