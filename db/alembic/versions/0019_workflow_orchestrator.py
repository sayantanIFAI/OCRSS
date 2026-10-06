"""Deterministic workflow orchestrator with bounded agent tools (backlog E12-S1, ADR-0003, §5).

* ``workflow_step``: every document transition, persisted. Each pipeline stage already opens and
  closes a ``pipeline_run`` (ingest, ocr, classify, extract, terminology, validate, project,
  agent); a trigger appends one step when a run finishes (``ok`` or ``failed``): the stage, its
  outputs (the run's metrics), the error, the document's status after it, the timing. No stage
  code changes. Append-only.
* ``workflow_replay``: a replay of a document's recorded steps (``cdi_adapter.orchestrator``): the
  plan it ran and each step's result. Append-only.
* ``agent_tool_call``: every call an agent makes through the tool registry - allow-listed,
  schema-validated, time-bounded - with its outcome (``ok``, or ``refused`` / ``invalid_input`` /
  ``invalid_output`` / ``timeout`` / ``error``). Append-only.
* the **agent governance guard**: in a database session marked as an agent's
  (``SET LOCAL cdi.actor_kind = 'agent'``, set by the tool runner), a ``clinical_fact`` can never
  become ``auto_accepted`` or ``clinician_confirmed`` and a FHIR bundle (``fhir_bundle``,
  ``cn_fhir_bundle``) can never become ``ready_to_share``. The S6 gate stays the only machine
  authority for accepting a fact (ADR-0003 rule 3); reviewers and the gate are not agent sessions.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-01
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_E = "integrity_constraint_violation"
_GUARDED = (
    ("clinical_fact", "review_state", "('auto_accepted', 'clinician_confirmed')"),
    ("fhir_bundle", "status", "('ready_to_share')"),
    ("cn_fhir_bundle", "status", "('ready_to_share')"),
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workflow_step (
            id               bigserial PRIMARY KEY,
            document_id      uuid,
            stage            text NOT NULL,
            status           text NOT NULL CHECK (status IN ('ok', 'failed')),
            pipeline_run_id  uuid,
            model_name       text,
            outputs          jsonb NOT NULL DEFAULT '{}'::jsonb,
            error            text,
            document_status  text,
            started_at       timestamptz,
            finished_at      timestamptz,
            recorded_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_workflow_step_document ON workflow_step (document_id, id)")
    op.execute(
        """
        CREATE FUNCTION workflow_step_from_run() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO workflow_step (document_id, stage, status, pipeline_run_id, model_name,
                                       outputs, error, document_status, started_at, finished_at)
            VALUES (NEW.document_id, NEW.stage, NEW.status, NEW.id, NEW.model_name, NEW.metrics,
                    NEW.error,
                    (SELECT d.status FROM source_document d WHERE d.id = NEW.document_id),
                    NEW.started_at, coalesce(NEW.finished_at, now()));
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER pipeline_run_to_workflow_step AFTER UPDATE OF status ON pipeline_run "
        "FOR EACH ROW WHEN (OLD.status = 'running' AND NEW.status <> 'running') "
        "EXECUTE FUNCTION workflow_step_from_run()"
    )
    op.execute(
        """
        CREATE TABLE workflow_replay (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id   uuid NOT NULL,
            from_stage    text,
            plan          jsonb NOT NULL,
            results       jsonb NOT NULL,
            status        text NOT NULL CHECK (status IN ('ok', 'failed')),
            requested_by  text NOT NULL,
            started_at    timestamptz NOT NULL,
            finished_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_tool_call (
            id           bigserial PRIMARY KEY,
            agent        text NOT NULL,
            tool         text NOT NULL,
            document_id  uuid,
            status       text NOT NULL CHECK (status IN ('ok', 'refused', 'invalid_input',
                             'invalid_output', 'timeout', 'error')),
            input        jsonb,
            output       jsonb,
            reason       text,
            duration_ms  integer,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_tool_call_agent ON agent_tool_call (agent, created_at)")
    op.execute(
        f"""
        CREATE FUNCTION orchestrator_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is an append-only workflow record (backlog E12-S1)', TG_TABLE_NAME
                USING ERRCODE = '{_E}';
        END $$
        """
    )
    for table in ("workflow_step", "workflow_replay", "agent_tool_call"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION orchestrator_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION orchestrator_append_only()"
        )
    # an agent's session can never set a governing state (ADR-0003 rule 3)
    op.execute(
        f"""
        CREATE FUNCTION agent_governance_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF current_setting('cdi.actor_kind', true) = 'agent' THEN
                RAISE EXCEPTION 'agent % may not set %.% (backlog E12-S1: the S6 gate and the '
                                'reviewer govern, agents only propose)',
                    coalesce(current_setting('cdi.agent', true), '?'), TG_TABLE_NAME,
                    TG_ARGV[0] USING ERRCODE = '{_E}';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    for table, col, states in _GUARDED:
        op.execute(
            f"CREATE TRIGGER {table}_agent_guard BEFORE INSERT OR UPDATE OF {col} ON {table} "
            f"FOR EACH ROW WHEN (NEW.{col} IN {states}) "
            f"EXECUTE FUNCTION agent_governance_guard('{col}')"
        )


def downgrade() -> None:
    for table, _, _ in _GUARDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_agent_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS agent_governance_guard()")
    op.execute("DROP TRIGGER IF EXISTS pipeline_run_to_workflow_step ON pipeline_run")
    op.execute("DROP FUNCTION IF EXISTS workflow_step_from_run()")
    for table in ("agent_tool_call", "workflow_replay", "workflow_step"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS orchestrator_append_only()")
