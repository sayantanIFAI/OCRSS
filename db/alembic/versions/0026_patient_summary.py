"""Derived patient summary, current with every governed fact change (backlog E5-S2; §4.1, §8.1).

The story asks for a *materialized* view that is current "within the same job's live-reprojection
window - no separate manual refresh step". PostgreSQL cannot do that with a materialized view:

* ``REFRESH MATERIALIZED VIEW`` rebuilds **every** patient and takes an ``AccessExclusiveLock`` on
  the view, so one patient's fact landing would block every reader and every other job - fatal at
  the 30-concurrent-patient load of ADR-0002;
* ``REFRESH MATERIALIZED VIEW CONCURRENTLY`` cannot run inside a transaction block at all, so it
  could not happen in the job's window even once.

So the summary is a **derived table maintained per patient**, refreshed for that one patient in the
same transaction as the fact that changed it (``cdi_adapter.summary``) - which is what the
acceptance actually asks for, and is O(one patient) instead of O(all patients):

* ``patient_summary``: one row per patient - the current problem list, active medications and
  allergies as jsonb, with their counts and the latest encounter date. It is built **from the
  §4.1 read-model views** (``v_problem_list``, ``v_medication_list``, ``v_allergy_list``), so it
  inherits E5-S1's continuity for free: the active order of each medicine, one line per problem.
* ``v_patient_summary`` reads it, so callers still read a view name and the storage can change.
* a trigger on ``clinical_fact`` sets ``stale_at`` whenever a **governed** fact is written,
  changed or removed. The refresh clears it. Nothing depends on the trigger for correctness - the
  refresh is in the same transaction - but it makes drift visible instead of silent
  (``summary.stale_patients``) and catches a future write path that forgets to refresh.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-05
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

GOVERNED = "('auto_accepted', 'clinician_confirmed', 'corrected')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE patient_summary (
            patient_id      uuid PRIMARY KEY REFERENCES patient_identity (id) ON DELETE CASCADE,
            problems        jsonb NOT NULL DEFAULT '[]'::jsonb,
            medications     jsonb NOT NULL DEFAULT '[]'::jsonb,
            allergies       jsonb NOT NULL DEFAULT '[]'::jsonb,
            problem_count   integer NOT NULL DEFAULT 0,
            medication_count integer NOT NULL DEFAULT 0,
            allergy_count   integer NOT NULL DEFAULT 0,
            last_encounter_date date,
            -- set by the trigger when a governed fact changes, cleared by the refresh: a row with
            -- stale_at set means some write path did not refresh, which is a bug, not a state
            stale_at        timestamptz,
            refreshed_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_patient_summary_stale ON patient_summary (stale_at) "
               "WHERE stale_at IS NOT NULL")  # fmt: skip
    op.execute(
        """
        CREATE VIEW v_patient_summary AS
        SELECT s.patient_id, p.mpi_id, p.name, p.sex, p.birth_date,
               s.problems, s.medications, s.allergies,
               s.problem_count, s.medication_count, s.allergy_count,
               s.last_encounter_date, s.refreshed_at, s.stale_at IS NOT NULL AS stale
        FROM patient_summary s
        JOIN patient_identity p ON p.id = s.patient_id
        """
    )
    # the trigger: a governed fact changing marks that patient's summary stale
    op.execute(
        f"""
        CREATE FUNCTION patient_summary_mark_stale() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE pid uuid;
        BEGIN
            pid := coalesce(NEW.patient_id, OLD.patient_id);
            IF pid IS NULL THEN
                RETURN NULL;
            END IF;
            IF (TG_OP <> 'DELETE' AND NEW.review_state IN {GOVERNED})
               OR (TG_OP <> 'INSERT' AND OLD.review_state IN {GOVERNED}) THEN
                UPDATE patient_summary SET stale_at = now() WHERE patient_id = pid;
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER patient_summary_mark_stale AFTER INSERT OR UPDATE OR DELETE "
        "ON clinical_fact FOR EACH ROW EXECUTE FUNCTION patient_summary_mark_stale()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS patient_summary_mark_stale ON clinical_fact")
    op.execute("DROP FUNCTION IF EXISTS patient_summary_mark_stale()")
    op.execute("DROP VIEW IF EXISTS v_patient_summary")
    op.execute("DROP TABLE IF EXISTS patient_summary")
