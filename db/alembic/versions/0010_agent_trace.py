"""Agent trace + the medication agent's pipeline stage (backlog E12-S5, ADR-0003 rule 5).

Owner request 2026-09-26: a medicine-diagnosis agent that re-reads each medicine line with the
prescription's diagnoses and the Tata 1mg catalogue, and suggests the product actually written
(backlog E12-S4, reviewer copilot). It is advisory only: it never sets a review state.

* ``agent_trace`` — every step an agent takes (model call, catalogue tool call, guard decision),
  in order, with its input and output: a document's full agent decision sequence can be rebuilt
  from this table alone. ``fact_id`` joins a step to the fact (and so to ``fact_provenance``) it is
  about; ``ai_event_id`` to the ``ai_event -> ai_suggestion`` record of the run's suggestions.
* ``pipeline_run.stage`` also allows ``'agent'`` (the run's status, model, timing and metrics).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-26
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_STAGES = "'ingest', 'ocr', 'classify', 'extract', 'terminology', 'validate', 'project'"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_trace (
            id              bigserial PRIMARY KEY,
            document_id     uuid NOT NULL REFERENCES source_document (id) ON DELETE CASCADE,
            run_id          uuid REFERENCES pipeline_run (id) ON DELETE CASCADE,
            ai_event_id     uuid REFERENCES ai_event (id) ON DELETE SET NULL,
            fact_id         uuid REFERENCES clinical_fact (id) ON DELETE SET NULL,
            seq             integer NOT NULL,
            agent           text NOT NULL,
            step            text NOT NULL,
            tool            text NOT NULL,
            input           jsonb NOT NULL DEFAULT '{}'::jsonb,
            output          jsonb NOT NULL DEFAULT '{}'::jsonb,
            confidence      real CHECK (confidence BETWEEN 0 AND 1),
            duration_ms     integer,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_trace_document ON agent_trace (document_id, run_id, seq)")
    op.execute("CREATE INDEX ix_agent_trace_fact ON agent_trace (fact_id)")
    op.execute("ALTER TABLE pipeline_run DROP CONSTRAINT pipeline_run_stage_check")
    op.execute(
        "ALTER TABLE pipeline_run ADD CONSTRAINT pipeline_run_stage_check "
        f"CHECK (stage IN ({_STAGES}, 'agent'))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_trace")
    op.execute("DELETE FROM pipeline_run WHERE stage = 'agent'")
    op.execute("ALTER TABLE pipeline_run DROP CONSTRAINT pipeline_run_stage_check")
    op.execute(
        "ALTER TABLE pipeline_run ADD CONSTRAINT pipeline_run_stage_check "
        f"CHECK (stage IN ({_STAGES}))"
    )
