"""A conflict's resolution, recorded on the conflict itself (backlog E5-S3; §5 S8, §4.1).

The S6 gate already writes ``fact_conflict`` when two facts of one patient disagree
(``value_mismatch``/``CONTRADICTED``/``blocker``, both facts held) - but the row carried only
``resolved``, so *who* settled it, *how*, and *what came out* lived nowhere. E5-S3's acceptance is
"**a single auditable decision**", so the decision is one row's worth of columns on the conflict:

* ``resolution``: ``keep_a`` | ``keep_b`` | ``merge`` - what the reviewer picked;
* ``result_fact_id``: for ``merge``, the corrected fact that came out of it (it supersedes the
  fact that was kept, exactly as a Reviewer correction does);
* ``resolved_by`` / ``resolved_at`` / ``note``: the signed-in reviewer, when, and why.

``resolved`` stays the flag the gate and the queue read, so nothing that looks at it changes.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-05
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE fact_conflict
            ADD COLUMN resolution     text CHECK (resolution IN ('keep_a', 'keep_b', 'merge')),
            ADD COLUMN result_fact_id uuid REFERENCES clinical_fact (id) ON DELETE SET NULL,
            ADD COLUMN resolved_by    text,
            ADD COLUMN resolved_at    timestamptz,
            ADD COLUMN note           text,
            -- a resolved conflict says how it was resolved and by whom; an open one says neither
            ADD CONSTRAINT fact_conflict_resolution_complete CHECK (
                (resolved AND resolution IS NOT NULL AND resolved_by IS NOT NULL
                 AND resolved_at IS NOT NULL)
                OR (NOT resolved AND resolution IS NULL AND resolved_by IS NULL
                    AND resolved_at IS NULL)),
            -- only a merge produces a new fact
            ADD CONSTRAINT fact_conflict_merge_result CHECK (
                result_fact_id IS NULL OR resolution = 'merge')
        """
    )
    op.execute("CREATE INDEX ix_fact_conflict_open ON fact_conflict (patient_id, created_at) "
               "WHERE NOT resolved")  # fmt: skip


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fact_conflict_open")
    op.execute(
        "ALTER TABLE fact_conflict "
        "  DROP CONSTRAINT IF EXISTS fact_conflict_merge_result, "
        "  DROP CONSTRAINT IF EXISTS fact_conflict_resolution_complete, "
        "  DROP COLUMN IF EXISTS note, "
        "  DROP COLUMN IF EXISTS resolved_at, "
        "  DROP COLUMN IF EXISTS resolved_by, "
        "  DROP COLUMN IF EXISTS result_fact_id, "
        "  DROP COLUMN IF EXISTS resolution"
    )
