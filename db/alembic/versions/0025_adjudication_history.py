"""Retain adjudication training snapshots across re-extraction (E4-S1/S7)."""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE calibration_adjudication (
            id bigserial PRIMARY KEY,
            proposed_fact_id uuid NOT NULL,
            final_fact_id uuid NOT NULL,
            example jsonb NOT NULL CHECK (jsonb_typeof(example) = 'object'),
            recorded_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_calibration_adjudication_root
            ON calibration_adjudication (proposed_fact_id, id DESC);
        CREATE FUNCTION calibration_adjudication_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'calibration_adjudication is immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END $$;
        CREATE TRIGGER calibration_adjudication_immutable
            BEFORE UPDATE OR DELETE ON calibration_adjudication
            FOR EACH ROW EXECUTE FUNCTION calibration_adjudication_immutable();
        CREATE TRIGGER calibration_adjudication_no_truncate
            BEFORE TRUNCATE ON calibration_adjudication
            FOR EACH STATEMENT EXECUTE FUNCTION calibration_adjudication_immutable();
    """)


def downgrade() -> None:
    op.execute("DROP TABLE calibration_adjudication")
    op.execute("DROP FUNCTION calibration_adjudication_immutable()")
