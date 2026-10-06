"""Close evidence removal, trace-reference and crop-pair integrity gaps (Epic 4).

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE gate_threshold ALTER COLUMN threshold TYPE double precision")
    # Serialize changes to a fact's evidence through a real parent-row update. This also
    # schedules 0012's deferred invariant and prevents write skew under repeatable read.
    op.execute("""
        CREATE FUNCTION provenance_recheck_fact() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE clinical_fact SET review_state = review_state WHERE id = OLD.fact_id;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER provenance_recheck_fact BEFORE DELETE OR UPDATE OF fact_id
            ON fact_provenance FOR EACH ROW EXECUTE FUNCTION provenance_recheck_fact();
        CREATE FUNCTION provenance_no_unwitnessed_truncate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM clinical_fact WHERE review_state = 'auto_accepted') THEN
                RAISE EXCEPTION 'cannot truncate evidence of auto_accepted facts'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$;
        CREATE TRIGGER provenance_no_unwitnessed_truncate BEFORE TRUNCATE ON fact_provenance
            FOR EACH STATEMENT EXECUTE FUNCTION provenance_no_unwitnessed_truncate();
    """)
    op.execute("""
        CREATE FUNCTION evidence_trace_references() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE candidate interpretation_candidate%ROWTYPE;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM unnest(NEW.observation_ids) AS ids(id)
                LEFT JOIN ocr_observation o ON o.id = ids.id
                WHERE o.id IS NULL OR o.document_id IS DISTINCT FROM NEW.document_id
            ) THEN
                RAISE EXCEPTION 'evidence observations must exist in the same document'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF TG_TABLE_NAME = 'verified_fact' THEN
                SELECT * INTO candidate FROM interpretation_candidate WHERE id = NEW.candidate_id;
                IF NOT FOUND OR candidate.fact_id IS DISTINCT FROM NEW.fact_id
                    OR candidate.document_id IS DISTINCT FROM NEW.document_id
                    OR candidate.observation_ids IS DISTINCT FROM NEW.observation_ids THEN
                    RAISE EXCEPTION 'verified fact must preserve its candidate evidence chain'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER interpretation_candidate_trace BEFORE INSERT ON interpretation_candidate
            FOR EACH ROW EXECUTE FUNCTION evidence_trace_references();
        CREATE TRIGGER verified_fact_trace BEFORE INSERT ON verified_fact
            FOR EACH ROW EXECUTE FUNCTION evidence_trace_references();
    """)
    op.execute("""
        CREATE FUNCTION ocr_agreement_same_crop() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE a ocr_observation%ROWTYPE; b ocr_observation%ROWTYPE;
        BEGIN
            SELECT * INTO a FROM ocr_observation WHERE id = NEW.observation_id_a;
            SELECT * INTO b FROM ocr_observation WHERE id = NEW.observation_id_b;
            IF a.id IS NULL OR b.id IS NULL THEN
                RAISE EXCEPTION 'both agreement observations must exist'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF a.document_id IS DISTINCT FROM b.document_id
                OR a.page_id IS DISTINCT FROM b.page_id
                OR a.crop_hash IS NULL OR b.crop_hash IS DISTINCT FROM a.crop_hash
                OR a.bbox IS DISTINCT FROM b.bbox
                OR a.engine IS NOT DISTINCT FROM b.engine
                OR a.region_kind IS DISTINCT FROM 'handwritten'
                OR b.region_kind IS DISTINCT FROM 'handwritten'
                OR (NEW.field_domain IS NOT NULL AND
                    (a.field_domain IS DISTINCT FROM NEW.field_domain
                     OR b.field_domain IS DISTINCT FROM NEW.field_domain)) THEN
                RAISE EXCEPTION 'agreement requires independent readings of the '
                                'same handwritten crop and field'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF btrim(NEW.algorithm_version) = '' THEN
                RAISE EXCEPTION 'algorithm_version must not be blank'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER ocr_agreement_same_crop BEFORE INSERT ON ocr_agreement
            FOR EACH ROW EXECUTE FUNCTION ocr_agreement_same_crop();
        CREATE UNIQUE INDEX ux_ocr_agreement_unordered_run ON ocr_agreement
            (least(observation_id_a, observation_id_b),
             greatest(observation_id_a, observation_id_b),
             coalesce(field_domain, ''), algorithm_version);
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE gate_threshold ALTER COLUMN threshold TYPE real")
    op.execute("DROP INDEX ux_ocr_agreement_unordered_run")
    op.execute("DROP TRIGGER ocr_agreement_same_crop ON ocr_agreement")
    op.execute("DROP FUNCTION ocr_agreement_same_crop()")
    op.execute("DROP TRIGGER verified_fact_trace ON verified_fact")
    op.execute("DROP TRIGGER interpretation_candidate_trace ON interpretation_candidate")
    op.execute("DROP FUNCTION evidence_trace_references()")
    op.execute("DROP TRIGGER provenance_recheck_fact ON fact_provenance")
    op.execute("DROP TRIGGER provenance_no_unwitnessed_truncate ON fact_provenance")
    op.execute("DROP FUNCTION provenance_recheck_fact()")
    op.execute("DROP FUNCTION provenance_no_unwitnessed_truncate()")
