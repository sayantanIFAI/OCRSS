"""Tests a prescription advises: ``clinical_fact.fact_type`` also allows ``'lab_order'``.

Owner request 2026-09-28: retrieve the lab tests written on a prescription (Investigations / Ix /
"Adv: CBC, LFT, HbA1c"). Each advised test is one ``lab_order`` fact, as written, bound to LOINC
(or SNOMED CT for an ordered study such as an X-ray or ECG) when the seed map knows it, reviewed in
the Reviewer, and carried into the EMR and the FHIR record as a ServiceRequest.

* ``clinical_fact.fact_type`` also allows ``'lab_order'``.
* ``cn_lab_order``: an advised test in the canonical EMR, promoted when its review item is
  approved (the same shared columns as every ``cn_*`` table of migration 0004).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-28
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_TYPES = (
    "'condition', 'symptom', 'finding', 'lab_result', 'vital_sign', 'procedure', 'medication', "
    "'allergy', 'immunization', 'diagnostic_report', 'advice', 'care_plan', 'referral', 'document'"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE cn_lab_order (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            patient_id      uuid NOT NULL REFERENCES patient_identity (id),
            rv_item_id      uuid REFERENCES rv_item (id),
            source_fact_id  uuid REFERENCES clinical_fact (id) ON DELETE SET NULL,
            encounter_id    uuid REFERENCES encounter (id) ON DELETE SET NULL,
            review_state    text NOT NULL,
            recorded_by     text NOT NULL,
            recorded_at     timestamptz NOT NULL DEFAULT now(),
            code_system     text,
            code            text,
            test_text       text NOT NULL,
            status          text NOT NULL DEFAULT 'active'
        )
        """
    )
    op.execute("CREATE INDEX ix_cn_lab_order_patient ON cn_lab_order (patient_id)")
    op.execute("ALTER TABLE clinical_fact DROP CONSTRAINT clinical_fact_fact_type_check")
    op.execute(
        "ALTER TABLE clinical_fact ADD CONSTRAINT clinical_fact_fact_type_check "
        f"CHECK (fact_type IN ({_TYPES}, 'lab_order'))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE cn_lab_order")
    op.execute("DELETE FROM clinical_fact WHERE fact_type = 'lab_order'")
    op.execute("ALTER TABLE clinical_fact DROP CONSTRAINT clinical_fact_fact_type_check")
    op.execute(
        "ALTER TABLE clinical_fact ADD CONSTRAINT clinical_fact_fact_type_check "
        f"CHECK (fact_type IN ({_TYPES}))"
    )
