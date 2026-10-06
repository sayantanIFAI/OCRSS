"""Gate thresholds derived per class from calibration data (backlog E4-S3, §5 S6, §15.10).

The S6 gate's auto-accept cut-offs, derived instead of hard-coded: per doc_type x fact_type x
field x evidence state, the lowest calibrated confidence at which the false-auto-accept rate stays
within the target, measured again on held-out adjudicated examples. ``cdi_adapter.training.
thresholds`` derives them from an E4-S2 calibrator and writes the set both here (the derivation
record) and as a JSON config file that is checked in; the gate reads the file named by
``CDI_GATE_THRESHOLDS`` (unset: the global constants, as before).

* ``gate_threshold_set``: one row per derived version - the calibrator it is on the scale of, the
  false-auto-accept targets (default and numeric fields) and where they come from, the bound used.
* ``gate_threshold``: one row per class - ``derived`` (its threshold, the measured rates),
  ``always_review`` (a rule: engines that disagree), ``insufficient_data`` or ``not_reachable``
  (an evidence state the pipeline does not produce yet): never a guessed number.

Both tables are append-only.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-30
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_E = "integrity_constraint_violation"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE gate_threshold_set (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            version             integer NOT NULL UNIQUE CHECK (version >= 1),
            calibrator_version  integer NOT NULL REFERENCES calibration_model (version),
            target_default      real NOT NULL CHECK (target_default > 0 AND target_default < 1),
            target_numeric      real NOT NULL CHECK (target_numeric > 0 AND target_numeric < 1),
            target_basis        text NOT NULL,
            bound               text NOT NULL,
            min_accepted        integer NOT NULL CHECK (min_accepted >= 1),
            summary             jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_by          text NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            CHECK (target_numeric <= target_default)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE gate_threshold (
            id                       bigserial PRIMARY KEY,
            set_id                   uuid NOT NULL REFERENCES gate_threshold_set (id),
            doc_type                 text NOT NULL,
            fact_type                text NOT NULL,
            field                    text NOT NULL,
            evidence_state           text NOT NULL,
            status                   text NOT NULL CHECK (status IN ('derived', 'always_review',
                                         'insufficient_data', 'not_reachable')),
            threshold                real CHECK (threshold BETWEEN 0 AND 1),
            target                   real NOT NULL,
            n_derive                 integer NOT NULL DEFAULT 0,
            n_accepted_derive        integer NOT NULL DEFAULT 0,
            false_accept_derive      real,
            n_accepted_held_out      integer NOT NULL DEFAULT 0,
            false_accept_held_out    real,
            meets_target             boolean,
            basis                    text NOT NULL,
            UNIQUE (set_id, doc_type, fact_type, field, evidence_state),
            CHECK ((status = 'derived') = (threshold IS NOT NULL))
        )
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION gate_threshold_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is a derived threshold set: it is never updated or deleted '
                            '(backlog E4-S3)', TG_TABLE_NAME USING ERRCODE = '{_E}';
        END $$
        """
    )
    for table in ("gate_threshold_set", "gate_threshold"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION gate_threshold_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION gate_threshold_append_only()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gate_threshold")
    op.execute("DROP TABLE IF EXISTS gate_threshold_set")
    op.execute("DROP FUNCTION IF EXISTS gate_threshold_append_only()")
