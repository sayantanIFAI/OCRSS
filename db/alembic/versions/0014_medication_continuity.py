"""Medication continuity across encounters (backlog E5-S1, ARCHITECTURE §5 S7).

A medication reordered or changed at a later encounter supersedes the earlier order: the active
medication list shows the latest order only; the earlier one is kept, in the history, linked.

* ``medication_supersession``: newer order -> the order it supersedes (both governed
  ``clinical_fact`` medications of one patient, from different encounters, the newer one strictly
  later), with what changed (``reordered`` | ``strength_changed`` | ``dose_changed`` |
  ``frequency_changed`` | ``duration_changed`` | ``changed``) and the match (bound code, else the
  normalized medicine name). Derived by ``cdi_adapter.longitudinal``; nothing is deleted from
  ``clinical_fact``. Kept apart from ``clinical_fact.supersedes``, which stays a reviewer's
  correction (the reviewer consoles follow that chain to a fact's current version).
* ``v_medication_list`` (the active list) leaves out a superseded order.
* ``v_medication_history``: every current medication order, active or superseded, with its link.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_OLD_ACTIVE_VIEW = """
CREATE OR REPLACE VIEW v_medication_list AS
SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
       coalesce(f.code_display, m.drug_text, f.local_text) AS medication,
       m.strength_num, m.strength_unit, m.dose_num, m.dose_unit_ucum, m.route,
       m.frequency_code, m.frequency_per_day, m.duration_days, m.prn, m.instructions,
       f.clinical_status, f.review_state, f.encounter_id, e.period_start AS encounter_date
FROM clinical_fact f
LEFT JOIN medication_detail m ON m.fact_id = f.id
LEFT JOIN encounter e ON e.id = f.encounter_id
WHERE f.fact_type = 'medication' AND f.is_current
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE medication_supersession (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            patient_id          uuid NOT NULL REFERENCES patient_identity (id),
            fact_id             uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
            supersedes_fact_id  uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
            match_key           text NOT NULL,
            change_kind         text NOT NULL CHECK (change_kind IN ('reordered',
                                    'strength_changed', 'dose_changed', 'frequency_changed',
                                    'duration_changed', 'changed')),
            encounter_date      date NOT NULL,
            superseded_date     date NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (fact_id, supersedes_fact_id),
            CHECK (fact_id <> supersedes_fact_id),
            CHECK (encounter_date > superseded_date)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_medication_supersession_old ON medication_supersession "
        "(supersedes_fact_id)"
    )
    op.execute(
        "CREATE INDEX ix_medication_supersession_patient ON medication_supersession (patient_id)"
    )
    # the active list: the latest order of each medication (a superseded order is history)
    op.execute(
        _OLD_ACTIVE_VIEW + "  AND NOT EXISTS (SELECT 1 FROM medication_supersession s "
        "WHERE s.supersedes_fact_id = f.id)"
    )
    op.execute(
        """
        CREATE VIEW v_medication_history AS
        SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
               coalesce(f.code_display, m.drug_text, f.local_text) AS medication,
               m.strength_num, m.strength_unit, m.dose_num, m.dose_unit_ucum, m.route,
               m.frequency_code, m.frequency_per_day, m.duration_days, m.prn, m.instructions,
               f.clinical_status, f.review_state, f.encounter_id,
               e.period_start AS encounter_date,
               CASE WHEN s.fact_id IS NULL THEN 'active' ELSE 'superseded' END AS order_status,
               s.fact_id AS superseded_by_fact_id, s.change_kind AS superseded_change,
               s.encounter_date AS superseded_on,
               (SELECT p.supersedes_fact_id FROM medication_supersession p
                WHERE p.fact_id = f.id ORDER BY p.superseded_date DESC LIMIT 1)
                   AS supersedes_fact_id
        FROM clinical_fact f
        LEFT JOIN medication_detail m ON m.fact_id = f.id
        LEFT JOIN encounter e ON e.id = f.encounter_id
        LEFT JOIN LATERAL (SELECT * FROM medication_supersession x
                           WHERE x.supersedes_fact_id = f.id
                           ORDER BY x.encounter_date LIMIT 1) s ON true
        WHERE f.fact_type = 'medication' AND f.is_current
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_medication_history")
    op.execute(_OLD_ACTIVE_VIEW)
    op.execute("DROP TABLE IF EXISTS medication_supersession")
