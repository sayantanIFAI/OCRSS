"""Durable web-app jobs (backlog E10-S9, with the core of E9-S1; ARCHITECTURE §7.2, §13).

  web_job   one row per upload job. The web app writes it, with the uploaded files already staged
            in the object store, BEFORE it acknowledges the upload (202): an acknowledged upload is
            never only in one process's memory. Any web-app instance reads it (the upload page
            polls whichever instance the balancer picks); the process that runs the job keeps it
            current (docs = the per-document status the page shows) and holds a lease on it
            (lease_owner / lease_until), renewed while it runs. A lease that runs out means that
            process died: another process claims the job and resumes it from the staged files.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-25
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

DDL = [
    """
    CREATE TABLE web_job (
        id               text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
        state            text NOT NULL DEFAULT 'running'
                             CHECK (state IN ('running', 'review', 'mismatch', 'error')),
        patient_id       uuid REFERENCES patient_identity (id) ON DELETE SET NULL,
        registry_patient jsonb,
        abha             text,
        mismatch         jsonb,
        files            jsonb NOT NULL,
        docs             jsonb NOT NULL,
        trace_id         text,
        span_id          text,
        created_by       text NOT NULL,
        accepted_by      text,
        created_at       timestamptz NOT NULL DEFAULT now(),
        started_at       timestamptz,
        finished_at      timestamptz,
        attempts         integer NOT NULL DEFAULT 0,
        lease_owner      text,
        lease_until      timestamptz,
        error            text,
        updated_at       timestamptz NOT NULL DEFAULT now(),
        -- a mismatch sets state while other documents may still run, so state alone does not
        -- mean "finished": finished_at does
        CHECK (finished_at IS NOT NULL OR state IN ('running', 'mismatch'))
    )
    """,
    # the reaper's scan: unfinished jobs only
    "CREATE INDEX ix_web_job_open ON web_job (lease_until) WHERE finished_at IS NULL",
    "CREATE INDEX ix_web_job_created ON web_job (created_at)",
]


def upgrade() -> None:
    for stmt in DDL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS web_job")
