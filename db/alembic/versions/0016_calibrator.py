"""Fitted confidence calibrators (backlog E4-S2, ARCHITECTURE §5 S6, §15.10).

An isotonic calibrator fitted on a published E4-S1 training version, per doc_type x fact_type -
per FIELD for prescription and lab-order facts (medicine, strength, dose, frequency, duration,
lab test, diagnosis). ``cdi_adapter.training.calibrator`` fits it; the S6 gate uses the version
named by ``CDI_CALIBRATOR_VERSION`` (unset: the fixed-penalty ``_calibrate``, as before).

* ``calibration_model``: one row per fitted version - the training version it learned from, the
  method, the minimum examples per slice, the reliability tolerance and where it comes from.
* ``calibration_slice``: one row per (doc_type, fact_type, field) - ``fitted`` with its curve, or
  ``insufficient_data`` (never guessed); the reliability diagram on held-out examples (bins of
  predicted vs observed correctness), the expected calibration error and whether it is within
  the tolerance.

Both tables are append-only: a fitted version is never changed.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-30
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_E = "integrity_constraint_violation"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE calibration_model (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            version          integer NOT NULL UNIQUE CHECK (version >= 1),
            dataset_version  integer NOT NULL REFERENCES calibration_dataset (version),
            method           text NOT NULL,
            min_examples     integer NOT NULL CHECK (min_examples >= 1),
            tolerance        real NOT NULL CHECK (tolerance > 0 AND tolerance < 1),
            tolerance_basis  text NOT NULL,
            summary          jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_by       text NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE calibration_slice (
            id                bigserial PRIMARY KEY,
            model_id          uuid NOT NULL REFERENCES calibration_model (id),
            doc_type          text NOT NULL,
            fact_type         text NOT NULL,
            field             text NOT NULL,
            status            text NOT NULL CHECK (status IN ('fitted', 'insufficient_data')),
            n_train           integer NOT NULL,
            n_test            integer NOT NULL,
            curve             jsonb,
            reliability       jsonb,
            ece_before        real,
            ece_after         real,
            within_tolerance  boolean,
            UNIQUE (model_id, doc_type, fact_type, field),
            CHECK ((status = 'fitted') = (curve IS NOT NULL))
        )
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION calibrator_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is a fitted calibrator: it is never updated or deleted '
                            '(backlog E4-S2)', TG_TABLE_NAME USING ERRCODE = '{_E}';
        END $$
        """
    )
    for table in ("calibration_model", "calibration_slice"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION calibrator_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION calibrator_append_only()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calibration_slice")
    op.execute("DROP TABLE IF EXISTS calibration_model")
    op.execute("DROP FUNCTION IF EXISTS calibrator_append_only()")
