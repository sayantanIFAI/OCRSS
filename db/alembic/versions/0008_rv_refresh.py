"""Workbasket refresh action (2026-09-25; ARCHITECTURE §4.5).

A document uploaded again (same bytes = same document) is extracted again: its old facts are
deleted (their ``rv_item_element`` rows go with them, ON DELETE CASCADE) and new ones written. The
document's open workbasket item is now brought up to date with the new facts instead of being left
empty, and that is recorded as an ``rv_action`` ``refresh``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_ACTIONS = "'create', 'claim', 'release', 'accept', 'correct', 'reject', 'approve', 'promote'"


def upgrade() -> None:
    op.execute("ALTER TABLE rv_action DROP CONSTRAINT rv_action_action_check")
    op.execute("ALTER TABLE rv_action ADD CONSTRAINT rv_action_action_check "
               f"CHECK (action IN ({_ACTIONS}, 'refresh'))")  # fmt: skip


def downgrade() -> None:
    op.execute("DELETE FROM rv_action WHERE action = 'refresh'")
    op.execute("ALTER TABLE rv_action DROP CONSTRAINT rv_action_action_check")
    op.execute("ALTER TABLE rv_action ADD CONSTRAINT rv_action_action_check "
               f"CHECK (action IN ({_ACTIONS}))")  # fmt: skip
