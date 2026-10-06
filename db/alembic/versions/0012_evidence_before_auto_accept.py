"""No evidence => cannot be auto_accepted, enforced by the database (backlog E4-S5).

ARCHITECTURE §5 S6: ``check_evidence`` is a blocker, so the gate never auto-accepts a fact with no
evidence. This makes that rule architecturally unbypassable: a code change elsewhere, or a direct
SQL write, cannot leave a ``clinical_fact`` in ``review_state = 'auto_accepted'`` with zero
``fact_provenance`` rows.

* A constraint trigger on ``clinical_fact`` (insert, or an update of ``review_state``), deferred to
  the end of the transaction: a fact and its provenance are written in one transaction, fact first
  (``extract/service.py``), and the S6 gate sets ``auto_accepted`` afterwards (``validate``).
* At commit it reads the fact's state then, not the state at the event: a fact set
  ``auto_accepted`` and held again in the same transaction is not refused.
* Rows already in the table are not rewritten or re-checked; the rule applies to every write from
  this revision on.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION clinical_fact_evidence_before_auto_accept() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM clinical_fact f
                       WHERE f.id = NEW.id AND f.review_state = 'auto_accepted')
               AND NOT EXISTS (SELECT 1 FROM fact_provenance p WHERE p.fact_id = NEW.id) THEN
                RAISE EXCEPTION 'clinical_fact % cannot be auto_accepted: it has no '
                                'fact_provenance row (no evidence, backlog E4-S5)', NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER clinical_fact_evidence_before_auto_accept
            AFTER INSERT OR UPDATE OF review_state ON clinical_fact
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW WHEN (NEW.review_state = 'auto_accepted')
            EXECUTE FUNCTION clinical_fact_evidence_before_auto_accept()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS clinical_fact_evidence_before_auto_accept ON clinical_fact")
    op.execute("DROP FUNCTION IF EXISTS clinical_fact_evidence_before_auto_accept()")
