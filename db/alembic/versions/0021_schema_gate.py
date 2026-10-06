"""Schema changes with workers in flight (backlog E9-S1, docs/ha.md "Application tier").

A forward-incompatible migration (a renamed or dropped column, a new constraint) breaks the SQL
of the workers that are already running. Two tables make the change safe without stopping the
cluster by hand:

* ``schema_gate``: one row. While it is closed, no runner takes a **new** job - the claim in
  ``web_job`` refuses it (``webapp/jobstore.py``) and the orphan sweep finds nothing - so the
  queue only holds still; nothing is lost, and nothing is run twice. ``expires_at`` bounds the
  hold: a migrator that dies never gates the queue for longer than its window.
* ``migration_run``: one append-only row per gated migration - direction, revisions, the jobs in
  flight when the gate closed, whether they drained, and the outcome (``applied``,
  ``rolled_back``, ``aborted`` or ``failed``).

The gate is held by ``cdi_adapter.migration``, which closes it, waits for the jobs in flight to
end, runs Alembic, and opens it again (also when the migration fails or is rolled back).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-03
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE schema_gate (
            id            smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            closed        boolean NOT NULL DEFAULT false,
            reason        text,
            actor         text,
            from_revision text,
            to_revision   text,
            closed_at     timestamptz,
            expires_at    timestamptz,
            updated_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("INSERT INTO schema_gate (id, closed) VALUES (1, false)")
    op.execute(
        """
        CREATE TABLE migration_run (
            id              bigserial PRIMARY KEY,
            direction       text NOT NULL CHECK (direction IN ('upgrade', 'downgrade')),
            target          text NOT NULL,
            from_revision   text,
            to_revision     text,
            actor           text NOT NULL,
            in_flight_at_close int NOT NULL DEFAULT 0,
            queued_at_close    int NOT NULL DEFAULT 0,
            drained         boolean NOT NULL DEFAULT false,
            drain_seconds   double precision,
            outcome         text NOT NULL DEFAULT 'running'
                            CHECK (outcome IN ('running', 'applied', 'rolled_back',
                                               'aborted', 'failed')),
            detail          text,
            started_at      timestamptz NOT NULL DEFAULT now(),
            finished_at     timestamptz
        )
        """
    )
    op.execute("CREATE INDEX migration_run_started_idx ON migration_run (started_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS migration_run")
    op.execute("DROP TABLE IF EXISTS schema_gate")
