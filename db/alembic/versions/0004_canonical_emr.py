"""Canonical EMR + review-workbasket layer (ARCHITECTURE §4.5).

Four groups on top of the per-document schema:
  cn_*          longitudinal, patient-scoped clinical record (read by Customer 360)
  ops_* / fin_* counter-side and billing-adjacent state (unfed until backlog E5-S4)
  ai_*          ai_event -> ai_suggestion -> ai_clinical_verification provenance chain
  rv_*          rv_item / rv_item_element / rv_action: the /reviewer workbasket model

§4.5 names the groups, the promotion path, cn_lab_result.test_code and the ai_/rv_ tables, but
gives no column lists (its own caveat: not re-derived from a live dump). Columns here are the
minimum the named workflow needs; backlog E1-S8 re-derives §4.5 from `pg_dump --schema-only`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-21
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_CN_COMMON = """
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      uuid NOT NULL REFERENCES patient_identity (id),
    rv_item_id      uuid REFERENCES rv_item (id),
    source_fact_id  uuid REFERENCES clinical_fact (id) ON DELETE SET NULL,
    encounter_id    uuid REFERENCES encounter (id) ON DELETE SET NULL,
    review_state    text NOT NULL,
    recorded_by     text NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now()
"""

DDL = [
    # ---------------------------------------------------------------- rv_* review / workbasket
    """
    CREATE TABLE rv_item (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        patient_id          uuid NOT NULL REFERENCES patient_identity (id),
        source_document_id  uuid REFERENCES source_document (id) ON DELETE CASCADE,
        job_id              text,
        doc_type            text,
        status              text NOT NULL DEFAULT 'workbasket' CHECK (status IN (
                                'workbasket', 'claimed', 'approved', 'rejected')),
        assigned_to         text,
        claimed_at          timestamptz,
        decided_at          timestamptz,
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_rv_item_status ON rv_item (status, created_at)",
    "CREATE INDEX ix_rv_item_assignee ON rv_item (assigned_to) WHERE assigned_to IS NOT NULL",
    """
    CREATE TABLE rv_item_element (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        rv_item_id      uuid NOT NULL REFERENCES rv_item (id) ON DELETE CASCADE,
        fact_id         uuid REFERENCES clinical_fact (id) ON DELETE CASCADE,
        element_type    text NOT NULL,
        payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
        state           text NOT NULL DEFAULT 'held' CHECK (state IN (
                            'held', 'accepted', 'corrected', 'rejected')),
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_rv_item_element_item ON rv_item_element (rv_item_id)",
    """
    CREATE TABLE rv_action (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        rv_item_id  uuid NOT NULL REFERENCES rv_item (id) ON DELETE CASCADE,
        actor       text NOT NULL,
        action      text NOT NULL CHECK (action IN (
                        'create', 'claim', 'release', 'accept', 'correct', 'reject',
                        'approve', 'promote')),
        detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
        at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_rv_action_item ON rv_action (rv_item_id, at)",
    # ---------------------------------------------------------------- ai_* provenance chain
    """
    CREATE TABLE ai_event (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        rv_item_id          uuid REFERENCES rv_item (id) ON DELETE CASCADE,
        source_document_id  uuid REFERENCES source_document (id) ON DELETE CASCADE,
        kind                text NOT NULL,                  -- extraction | classification | ...
        model_stack         jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE ai_suggestion (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        ai_event_id     uuid NOT NULL REFERENCES ai_event (id) ON DELETE CASCADE,
        fact_id         uuid REFERENCES clinical_fact (id) ON DELETE SET NULL,
        element_type    text NOT NULL,
        suggested       jsonb NOT NULL,
        confidence      real CHECK (confidence BETWEEN 0 AND 1),
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE ai_clinical_verification (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        ai_suggestion_id uuid NOT NULL REFERENCES ai_suggestion (id) ON DELETE CASCADE,
        reviewer        text NOT NULL,
        decision        text NOT NULL CHECK (decision IN ('accepted', 'corrected', 'rejected')),
        final_value     jsonb,
        decided_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    # ---------------------------------------------------------------- cn_* canonical clinical
    f"""
    CREATE TABLE cn_condition ({_CN_COMMON},
        code_system text, code text, display text NOT NULL,
        clinical_status text, verification text, onset timestamptz)
    """,
    f"""
    CREATE TABLE cn_medication ({_CN_COMMON},
        code_system text, code text, drug_text text NOT NULL,
        strength_num numeric, strength_unit text, dose_num numeric, dose_unit_ucum text,
        route text, frequency_code text, frequency_per_day numeric, duration_days numeric,
        instructions text, status text NOT NULL DEFAULT 'active')
    """,
    # §4.5: the test-code column is test_code, not code
    f"""
    CREATE TABLE cn_lab_result ({_CN_COMMON},
        test_system text, test_code text, test_display text NOT NULL,
        value_num numeric, value_text text, unit_ucum text,
        ref_range_low numeric, ref_range_high numeric, ref_range_text text,
        abnormal_flag text, effective_time timestamptz)
    """,
    f"""
    CREATE TABLE cn_vital_sign ({_CN_COMMON},
        code_system text, code text, display text NOT NULL,
        value_num numeric, value_text text, unit_ucum text, effective_time timestamptz)
    """,
    f"""
    CREATE TABLE cn_allergy ({_CN_COMMON},
        code_system text, code text, substance text NOT NULL, reaction text,
        clinical_status text)
    """,
    # unfed today: the voice-AI dependency is backlog E5-S4
    """
    CREATE TABLE cn_clinical_note (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        patient_id  uuid NOT NULL REFERENCES patient_identity (id),
        encounter_id uuid REFERENCES encounter (id) ON DELETE SET NULL,
        note_type   text NOT NULL,
        body        text NOT NULL,
        author      text,
        source      text,
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE cn_fhir_bundle (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        patient_id          uuid NOT NULL REFERENCES patient_identity (id),
        rv_item_id          uuid REFERENCES rv_item (id) ON DELETE SET NULL,
        artifact_type       text NOT NULL,
        bundle              jsonb NOT NULL,
        bundle_hash         char(64) NOT NULL,
        validation          jsonb NOT NULL DEFAULT '{}'::jsonb,
        status              text NOT NULL DEFAULT 'draft' CHECK (status IN (
                                'draft', 'validated', 'ready_to_share', 'shared', 'superseded')),
        created_at          timestamptz NOT NULL DEFAULT now()
    )
    """,
    # ---------------------------------------------------------------- ops_* / fin_* (unfed: E5-S4)
    """
    CREATE TABLE ops_patient_visit (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        patient_id  uuid NOT NULL REFERENCES patient_identity (id),
        visit_date  date NOT NULL DEFAULT current_date,
        counter     text,
        status      text NOT NULL DEFAULT 'open',
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE fin_billing_item (
        id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        patient_id      uuid NOT NULL REFERENCES patient_identity (id),
        visit_id        uuid REFERENCES ops_patient_visit (id) ON DELETE SET NULL,
        source_cn_table text,
        source_cn_id    uuid,
        description     text NOT NULL,
        amount          numeric(12, 2),
        status          text NOT NULL DEFAULT 'pending',
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
]

CN_TABLES = ("cn_condition", "cn_medication", "cn_lab_result", "cn_vital_sign", "cn_allergy")

DROP_ORDER = (
    "fin_billing_item",
    "ops_patient_visit",
    "cn_fhir_bundle",
    "cn_clinical_note",
    *reversed(CN_TABLES),
    "ai_clinical_verification",
    "ai_suggestion",
    "ai_event",
    "rv_action",
    "rv_item_element",
    "rv_item",
)


def upgrade() -> None:
    for stmt in DDL:
        op.execute(stmt)
    for t in CN_TABLES:
        op.execute(f"CREATE INDEX ix_{t}_patient ON {t} (patient_id)")


def downgrade() -> None:
    for t in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {t}")
