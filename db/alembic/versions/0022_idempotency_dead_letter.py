"""Idempotency keys, priority lanes and the dead-letter queue (backlog E9-S3, §5 S1, §14 #8).

* ``web_job.idempotency_key`` + a unique index: one accepted upload, one job. The key is the
  client's ``Idempotency-Key`` header, or - when it sends none - derived from the upload itself
  (``webapp/jobstore.idempotency_key``), so a counter that retries a timed-out multipart POST with
  the same bytes gets the *first* job back instead of a second one. The index is what makes it
  safe when both copies arrive at once: the loser of the insert reads the winner's job.
  ``§5 S1``'s SHA-256 ingest dedupe already shares the *documents*; this stops the second *job*.
* ``web_job.priority``: the lane a job is dispatched on (0 = lowest … 9 = highest, 5 = normal),
  carried to Celery as the message priority.
* ``dead_letter``: what stopped being retried - a job that kept killing its runner, a document
  (or any task) that failed every attempt - with its last error and attempt count, so a poison
  document leaves the queue instead of blocking it and can still be found and replayed. One row
  per (kind, ref): a later failure updates it rather than piling up.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-03
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE web_job ADD COLUMN idempotency_key text")
    op.execute(
        "CREATE UNIQUE INDEX ux_web_job_idempotency ON web_job (idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE web_job ADD COLUMN priority smallint NOT NULL DEFAULT 5 "
        "CHECK (priority BETWEEN 0 AND 9)"
    )
    op.execute("ALTER TABLE web_job ADD COLUMN dead_lettered_at timestamptz")
    op.execute(
        """
        CREATE TABLE dead_letter (
            id          bigserial PRIMARY KEY,
            kind        text NOT NULL CHECK (kind IN ('web_job', 'document', 'task')),
            ref         text NOT NULL,
            queue       text,
            task        text,
            attempts    integer NOT NULL DEFAULT 0,
            error       text,
            detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
            first_at    timestamptz NOT NULL DEFAULT now(),
            at          timestamptz NOT NULL DEFAULT now(),
            replayed_at timestamptz,
            replayed_by text,
            UNIQUE (kind, ref)
        )
        """
    )
    op.execute("CREATE INDEX ix_dead_letter_open ON dead_letter (at DESC) "
               "WHERE replayed_at IS NULL")  # fmt: skip


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dead_letter")
    op.execute("ALTER TABLE web_job DROP COLUMN IF EXISTS dead_lettered_at")
    op.execute("ALTER TABLE web_job DROP COLUMN IF EXISTS priority")
    op.execute("DROP INDEX IF EXISTS ux_web_job_idempotency")
    op.execute("ALTER TABLE web_job DROP COLUMN IF EXISTS idempotency_key")
