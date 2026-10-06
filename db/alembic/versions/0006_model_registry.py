"""Model registry + canary rollout log (backlog E10-S5; ARCHITECTURE §14 #9).

  model_version   one row per registered model version: what the gateway loads (model id,
                  pinned revision, backend, quantization) plus its E2-S7 offline eval report
  model_rollout   append-only log of rollout states: after every deploy / canary / percent change /
                  promote / rollback, who did it, why, and which versions serve at which gateway.
                  The latest row is the live routing state; UPDATE and DELETE are refused, so the
                  history a rollback reads can never be rewritten

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE model_version (
        version         text PRIMARY KEY CHECK (version ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,79}$'),
        model_id        text NOT NULL,
        revision        text NOT NULL,
        backend         text NOT NULL CHECK (backend IN ('stub', 'hf', 'vllm')),
        quantization    text,
        eval_report     jsonb,
        notes           text,
        registered_by   text NOT NULL,
        registered_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE model_rollout (
        id              bigserial PRIMARY KEY,
        at              timestamptz NOT NULL DEFAULT now(),
        actor           text NOT NULL,
        action          text NOT NULL CHECK (action IN
                            ('deploy', 'canary', 'set_percent', 'promote', 'rollback')),
        primary_version text NOT NULL REFERENCES model_version (version),
        primary_url     text NOT NULL,
        canary_version  text REFERENCES model_version (version),
        canary_url      text,
        canary_percent  smallint NOT NULL DEFAULT 0 CHECK (canary_percent BETWEEN 0 AND 100),
        reason          text,
        CHECK ((canary_version IS NULL) = (canary_url IS NULL)),
        CHECK (canary_version IS NOT NULL OR canary_percent = 0),
        CHECK (canary_version IS DISTINCT FROM primary_version)
    )
    """,
    """
    CREATE FUNCTION model_rollout_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'model_rollout is append-only (backlog E10-S5)';
    END $$
    """,
    """
    CREATE TRIGGER model_rollout_no_rewrite BEFORE UPDATE OR DELETE ON model_rollout
        FOR EACH ROW EXECUTE FUNCTION model_rollout_append_only()
    """,
    "CREATE INDEX ix_extraction_model_version ON extraction (model_version, created_at)",
]


def upgrade() -> None:
    for stmt in DDL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_extraction_model_version")
    op.execute("DROP TABLE IF EXISTS model_rollout")
    op.execute("DROP FUNCTION IF EXISTS model_rollout_append_only()")
    op.execute("DROP TABLE IF EXISTS model_version")
