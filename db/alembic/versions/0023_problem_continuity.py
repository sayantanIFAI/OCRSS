"""Problem-list merge across encounters (backlog E5-S1, the second half; §13, §4.1).

0014 gave medications their continuity. A **problem** is not reordered, it is *mentioned again*:
the same condition recorded at three encounters is one problem on the list, not three. So the
same discipline, with the link read the other way round:

* ``problem_continuity``: a later mention **continues** the latest earlier mention of the same
  problem (same bound code, else the same normalized text). Governed facts only; nothing is
  deleted from ``clinical_fact``; every link is in ``audit_log``.
* ``v_problem_list`` (the problem list) now shows the **latest mention** of each problem, with
  ``first_onset`` (the earliest onset or encounter date in its chain) and how many mentions it
  has - so a condition carried across encounters reads as one line with its history behind it,
  and the clinician sees the **current** status, not the first one recorded.
* ``v_problem_history``: every current condition fact, active or continued, with its link.

``clinical_fact.supersedes`` stays untouched: it is a reviewer's correction, not a timeline.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-03
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# the problem list as 0001 defined it; the new definition appends columns and hides a mention
# that a later one continues (appending is all CREATE OR REPLACE VIEW allows)
_COLUMNS = """
SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
       coalesce(f.code_display, f.local_text) AS problem,
       f.clinical_status, f.verification, f.onset, f.review_state, f.encounter_id,
       e.period_start AS encounter_date
FROM clinical_fact f
LEFT JOIN encounter e ON e.id = f.encounter_id
WHERE f.fact_type = 'condition' AND f.is_current
"""

_ACTIVE = """
CREATE OR REPLACE VIEW v_problem_list AS
SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
       coalesce(f.code_display, f.local_text) AS problem,
       f.clinical_status, f.verification, f.onset, f.review_state, f.encounter_id,
       e.period_start AS encounter_date,
       -- the whole chain's earliest onset, so a carried-over problem keeps its real start
       coalesce((SELECT min(coalesce(g.onset::date, ge.period_start))
                 FROM problem_continuity c
                 JOIN clinical_fact g ON g.id = c.continues_fact_id
                 LEFT JOIN encounter ge ON ge.id = g.encounter_id
                 WHERE c.chain_head_fact_id = coalesce(
                     (SELECT h.chain_head_fact_id FROM problem_continuity h
                      WHERE h.fact_id = f.id), f.id)),
                f.onset::date, e.period_start) AS first_onset,
       1 + (SELECT count(*) FROM problem_continuity c
            WHERE c.chain_head_fact_id = coalesce(
                (SELECT h.chain_head_fact_id FROM problem_continuity h WHERE h.fact_id = f.id),
                f.id)) AS mentions
FROM clinical_fact f
LEFT JOIN encounter e ON e.id = f.encounter_id
WHERE f.fact_type = 'condition' AND f.is_current
  AND NOT EXISTS (SELECT 1 FROM problem_continuity s WHERE s.continues_fact_id = f.id)
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE problem_continuity (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            patient_id          uuid NOT NULL REFERENCES patient_identity (id),
            fact_id             uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
            continues_fact_id   uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
            -- the earliest mention of this problem: what the chain is counted and dated from
            chain_head_fact_id  uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
            match_key           text NOT NULL,
            status_change       text NOT NULL CHECK (status_change IN ('unchanged',
                                    'status_changed', 'verification_changed', 'changed')),
            encounter_date      date NOT NULL,
            continued_date      date NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            UNIQUE (fact_id, continues_fact_id),
            CHECK (fact_id <> continues_fact_id),
            CHECK (encounter_date > continued_date)
        )
        """
    )
    op.execute("CREATE INDEX ix_problem_continuity_earlier ON problem_continuity "
               "(continues_fact_id)")  # fmt: skip
    op.execute("CREATE INDEX ix_problem_continuity_patient ON problem_continuity (patient_id)")
    op.execute("CREATE INDEX ix_problem_continuity_chain ON problem_continuity "
               "(chain_head_fact_id)")  # fmt: skip
    op.execute(_ACTIVE)
    op.execute(
        """
        CREATE VIEW v_problem_history AS
        SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
               coalesce(f.code_display, f.local_text) AS problem,
               f.clinical_status, f.verification, f.onset, f.review_state, f.encounter_id,
               e.period_start AS encounter_date,
               CASE WHEN s.fact_id IS NULL THEN 'current' ELSE 'continued' END AS mention_status,
               s.fact_id AS continued_by_fact_id, s.status_change AS continued_change,
               s.encounter_date AS continued_on,
               coalesce(c.chain_head_fact_id, f.id) AS chain_head_fact_id,
               c.continues_fact_id
        FROM clinical_fact f
        LEFT JOIN encounter e ON e.id = f.encounter_id
        LEFT JOIN LATERAL (SELECT * FROM problem_continuity x
                           WHERE x.continues_fact_id = f.id
                           ORDER BY x.encounter_date LIMIT 1) s ON true
        LEFT JOIN LATERAL (SELECT * FROM problem_continuity y
                           WHERE y.fact_id = f.id
                           ORDER BY y.continued_date DESC LIMIT 1) c ON true
        WHERE f.fact_type = 'condition' AND f.is_current
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_problem_history")
    op.execute("DROP VIEW IF EXISTS v_problem_list")
    op.execute("DROP TABLE IF EXISTS problem_continuity")
    op.execute("CREATE OR REPLACE VIEW v_problem_list AS" + _COLUMNS)
