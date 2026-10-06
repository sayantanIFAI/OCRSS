"""ocr_agreement: the cross-engine concordance signal (backlog E4-S11, companion to E4-S7).

Where E2-S11's disagreement engine writes its result: for one handwritten crop, whether two
independent recognizer readings (TrOCR and Qwen, each an ``ocr_observation`` row) agree.

* one row per crop (and field domain) per algorithm version: ``disagreement_state`` AGREE /
  PARTIAL / MATERIAL, an ``agreement_score``, each engine's confidence, the reason, the version
  of the comparison and when it ran;
* ``observation_id_a`` / ``observation_id_b`` are foreign keys into ``ocr_observation``;
* append-only, the evidence tables' own discipline: a row is never updated or deleted; a re-run
  writes a new row under a new ``algorithm_version`` (the same pair, field and version twice is
  refused).

Deliberately only the concordance signal: no join to ``interpretation_candidate`` is built here,
and the table must not grow into a second candidate table.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-01
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_E = "integrity_constraint_violation"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ocr_agreement (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            observation_id_a    uuid NOT NULL REFERENCES ocr_observation (id),
            observation_id_b    uuid NOT NULL REFERENCES ocr_observation (id),
            field_domain        text,
            disagreement_state  text NOT NULL CHECK (disagreement_state IN
                                    ('AGREE', 'PARTIAL', 'MATERIAL')),
            agreement_score     real NOT NULL CHECK (agreement_score BETWEEN 0 AND 1),
            trocr_confidence    real CHECK (trocr_confidence BETWEEN 0 AND 1),
            qwen_confidence     real CHECK (qwen_confidence BETWEEN 0 AND 1),
            reason              text,
            algorithm_version   text NOT NULL CHECK (algorithm_version <> ''),
            computed_at         timestamptz NOT NULL DEFAULT now(),
            CHECK (observation_id_a <> observation_id_b)
        )
        """
    )
    # one result per crop pair, field and comparison version: a re-run needs a new version
    op.execute(
        "CREATE UNIQUE INDEX ux_ocr_agreement_run ON ocr_agreement "
        "(observation_id_a, observation_id_b, coalesce(field_domain, ''), algorithm_version)"
    )
    op.execute("CREATE INDEX ix_ocr_agreement_b ON ocr_agreement (observation_id_b)")
    op.execute(
        f"""
        CREATE FUNCTION ocr_agreement_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'ocr_agreement is append-only: a result is never updated or '
                            'deleted; a re-run writes a new algorithm_version (backlog E4-S11)'
                USING ERRCODE = '{_E}';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER ocr_agreement_append_only BEFORE UPDATE OR DELETE ON ocr_agreement "
        "FOR EACH ROW EXECUTE FUNCTION ocr_agreement_append_only()"
    )
    op.execute(
        "CREATE TRIGGER ocr_agreement_no_truncate BEFORE TRUNCATE ON ocr_agreement "
        "FOR EACH STATEMENT EXECUTE FUNCTION ocr_agreement_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ocr_agreement")
    op.execute("DROP FUNCTION IF EXISTS ocr_agreement_append_only()")
