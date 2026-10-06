"""Agent trace: auditable like the deterministic pipeline (backlog E12-S5, §4.3, ADR-0003 rule 5).

``agent_trace`` (migration 0010) already records every agent step in order. This makes it a
record that can be relied on to rebuild a document's agent decisions:

* **append-only** - a trace step is never updated or deleted (nor truncated). The only change
  allowed is the database's own ``ON DELETE SET NULL`` on ``fact_id`` / ``ai_event_id`` when a fact
  is purged by a re-extraction; deleting a document or a run that has agent steps is refused.
* ``fact_ref`` - the fact a step was about, as recorded (copied from ``fact_id`` on insert). It is
  kept when re-extraction purges the fact, so the step still names the fact it decided on; for a
  current fact it equals ``fact_id`` and joins to that fact's ``fact_provenance`` rows.
* ``tool_call_id`` - a step that is a tool-registry call (E12-S1) points at its ``agent_tool_call``
  row (status, timing).
* **audit_log** - every step is also written to ``audit_log`` (``entity = 'agent_trace'``,
  ``entity_id`` = the step id, ``actor`` = the agent), the same log the pipeline and reviewers
  write, so an agent decision shows up wherever pipeline and reviewer actions are audited.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-01
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_E = "integrity_constraint_violation"


def upgrade() -> None:
    op.execute("ALTER TABLE agent_trace ADD COLUMN fact_ref uuid")
    op.execute(
        "ALTER TABLE agent_trace ADD COLUMN tool_call_id bigint REFERENCES agent_tool_call (id)"
    )
    op.execute("UPDATE agent_trace SET fact_ref = fact_id WHERE fact_id IS NOT NULL")
    op.execute("CREATE INDEX ix_agent_trace_fact_ref ON agent_trace (fact_ref)")
    op.execute(
        """
        CREATE FUNCTION agent_trace_on_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.fact_ref := coalesce(NEW.fact_ref, NEW.fact_id);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_trace_on_insert BEFORE INSERT ON agent_trace "
        "FOR EACH ROW EXECUTE FUNCTION agent_trace_on_insert()"
    )
    op.execute(
        """
        CREATE FUNCTION agent_trace_to_audit_log() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO audit_log (actor, action, entity, entity_id, detail)
            VALUES (NEW.agent, 'create', 'agent_trace', NEW.id::text,
                    jsonb_build_object('document_id', NEW.document_id, 'run_id', NEW.run_id,
                                       'fact_id', NEW.fact_ref, 'seq', NEW.seq,
                                       'step', NEW.step, 'tool', NEW.tool,
                                       'confidence', NEW.confidence,
                                       'tool_call_id', NEW.tool_call_id));
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_trace_to_audit_log AFTER INSERT ON agent_trace "
        "FOR EACH ROW EXECUTE FUNCTION agent_trace_to_audit_log()"
    )
    op.execute(
        f"""
        CREATE FUNCTION agent_trace_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            kept agent_trace%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'agent_trace % is an audit record: it is never deleted '
                                '(backlog E12-S5)', OLD.id USING ERRCODE = '{_E}';
            END IF;
            -- only the ON DELETE SET NULL of a purged fact / ai_event may change a step
            kept := NEW;
            IF NEW.fact_id IS NULL THEN kept.fact_id := OLD.fact_id; END IF;
            IF NEW.ai_event_id IS NULL THEN kept.ai_event_id := OLD.ai_event_id; END IF;
            IF kept IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'agent_trace % is an audit record: it is never updated '
                                '(backlog E12-S5)', OLD.id USING ERRCODE = '{_E}';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_trace_append_only BEFORE UPDATE OR DELETE ON agent_trace "
        "FOR EACH ROW EXECUTE FUNCTION agent_trace_append_only()"
    )
    op.execute(
        f"""
        CREATE FUNCTION agent_trace_no_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'agent_trace is an audit record: it is never truncated '
                            '(backlog E12-S5)' USING ERRCODE = '{_E}';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_trace_no_truncate BEFORE TRUNCATE ON agent_trace "
        "FOR EACH STATEMENT EXECUTE FUNCTION agent_trace_no_truncate()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS agent_trace_no_truncate ON agent_trace")
    op.execute("DROP FUNCTION IF EXISTS agent_trace_no_truncate()")
    op.execute("DROP TRIGGER IF EXISTS agent_trace_append_only ON agent_trace")
    op.execute("DROP FUNCTION IF EXISTS agent_trace_append_only()")
    op.execute("DROP TRIGGER IF EXISTS agent_trace_to_audit_log ON agent_trace")
    op.execute("DROP FUNCTION IF EXISTS agent_trace_to_audit_log()")
    op.execute("DROP TRIGGER IF EXISTS agent_trace_on_insert ON agent_trace")
    op.execute("DROP FUNCTION IF EXISTS agent_trace_on_insert()")
    op.execute("DROP INDEX IF EXISTS ix_agent_trace_fact_ref")
    op.execute("ALTER TABLE agent_trace DROP COLUMN IF EXISTS tool_call_id")
    op.execute("ALTER TABLE agent_trace DROP COLUMN IF EXISTS fact_ref")
