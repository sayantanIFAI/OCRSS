"""Immutable 3-table evidence schema (backlog E4-S7, ARCHITECTURE §15.4, HW-Phase A).

"Never overwrite raw evidence" enforced by the database, the same pattern as E4-S5:

* ``ocr_observation`` (was ``ocr_block``): what a recognizer saw, append-only. Its raw columns can
  never be updated and no row can be deleted (nor the table truncated). New columns: ``crop_hash``
  (SHA-256 of the pixels the engine read, cropped from the page image at ``bbox``),
  ``region_kind``, ``field_domain``, ``engine_version``, ``prompt_hash``, ``token_confidences``,
  and ``superseded_by`` / ``superseded_at``: a later engine run no longer deletes the earlier
  observations (S3's handwritten VLM pass used to delete the RapidOCR blocks), it points them at
  itself. ``text`` is renamed ``raw_text``; ``ocr_conf`` is the raw confidence and
  ``pipeline_run_id`` the run. Recognition only: no code, code system or concept column.
* ``ocr_block`` is now a view of the current (not superseded) observations with the old column
  names, so every reader - extraction, ``fact_provenance.ocr_block_ids``, the S8 ``/review`` and
  ``/reviewer`` consoles - works unchanged.
* ``interpretation_candidate``: a scored, sourced normalization of one or more observations
  (concept, code, source, ``resolved_by_level``, the evidence matrix, ``eliminated_by``). Rows
  are never deleted; only ``eliminated_by`` may be set, once.
* ``verified_fact``: the final, governed value (observation ids, the winning candidate,
  ``verification_method``, calibrated confidence, ``policy_id``, ``reviewer_id``,
  ``model_version``). Never updated or deleted, so re-running extraction (which rewrites
  ``clinical_fact``) never purges it or a reviewer's decision. ``fact_id`` is a plain pointer to
  the ``clinical_fact`` version it verified, deliberately without a foreign key for that reason.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_E = "integrity_constraint_violation"


def upgrade() -> None:
    # ------------------------------------------------------------------ ocr_observation
    op.execute("ALTER TABLE ocr_block RENAME TO ocr_observation")
    op.execute("ALTER TABLE ocr_observation RENAME COLUMN text TO raw_text")
    op.execute("ALTER INDEX ix_ocr_block_page RENAME TO ix_ocr_observation_page")
    op.execute("ALTER INDEX ix_ocr_block_fts RENAME TO ix_ocr_observation_fts")
    op.execute(
        """
        ALTER TABLE ocr_observation
            ADD COLUMN crop_hash         char(64),
            ADD COLUMN region_kind       text CHECK (region_kind IN
                                             ('printed', 'handwritten', 'mixed', 'uncertain')),
            ADD COLUMN field_domain      text,
            ADD COLUMN engine_version    text,
            ADD COLUMN prompt_hash       char(64),
            ADD COLUMN token_confidences jsonb,
            ADD COLUMN superseded_by     uuid REFERENCES pipeline_run (id),
            ADD COLUMN superseded_at     timestamptz
        """
    )
    op.execute(
        "CREATE INDEX ix_ocr_observation_current ON ocr_observation (document_id) "
        "WHERE superseded_by IS NULL"
    )
    op.execute(
        f"""
        CREATE FUNCTION ocr_observation_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            kept ocr_observation%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'ocr_observation % is raw evidence: it is never deleted '
                                '(backlog E4-S7)', OLD.id USING ERRCODE = '{_E}';
            END IF;
            IF OLD.superseded_by IS NOT NULL THEN
                RAISE EXCEPTION 'ocr_observation % is already superseded (backlog E4-S7)', OLD.id
                    USING ERRCODE = '{_E}';
            END IF;
            -- only the supersession pointer may change: every raw column stays as recorded
            kept := NEW;
            kept.superseded_by := OLD.superseded_by;
            kept.superseded_at := OLD.superseded_at;
            IF kept IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'ocr_observation % is raw evidence: it is never updated '
                                '(backlog E4-S7)', OLD.id USING ERRCODE = '{_E}';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER ocr_observation_append_only BEFORE UPDATE OR DELETE ON ocr_observation "
        "FOR EACH ROW EXECUTE FUNCTION ocr_observation_append_only()"
    )
    op.execute(
        f"""
        CREATE FUNCTION evidence_no_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is evidence: it is never truncated (backlog E4-S7)',
                TG_TABLE_NAME USING ERRCODE = '{_E}';
        END $$
        """
    )
    # the old name, for every reader: the current observations, the old column names
    op.execute(
        """
        CREATE VIEW ocr_block AS
            SELECT id, document_id, page_id, block_type, raw_text AS text, bbox, polygon,
                   ocr_conf, reading_order, table_ref, engine, pipeline_run_id, created_at
            FROM ocr_observation WHERE superseded_by IS NULL
        """
    )

    # ------------------------------------------------------------------ interpretation_candidate
    op.execute(
        """
        CREATE TABLE interpretation_candidate (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id       uuid REFERENCES source_document (id),
            fact_id           uuid,
            observation_ids   uuid[] NOT NULL DEFAULT '{}',
            field             text NOT NULL,
            value_text        text,
            value_num         numeric,
            unit              text,
            concept           text,
            code_system       text,
            code              text,
            source            text NOT NULL CHECK (source IN
                                  ('extractor', 'terminology', 'catalogue', 'agent', 'reviewer')),
            resolved_by_level text,
            score             real CHECK (score BETWEEN 0 AND 1),
            evidence          jsonb NOT NULL DEFAULT '{}'::jsonb,
            eliminated_by     text,
            model_version     text,
            pipeline_run_id   uuid,
            created_by        text NOT NULL DEFAULT 'system',
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_interpretation_candidate_fact ON interpretation_candidate (fact_id)"
    )
    op.execute(
        "CREATE INDEX ix_interpretation_candidate_doc ON interpretation_candidate (document_id)"
    )
    op.execute(
        f"""
        CREATE FUNCTION interpretation_candidate_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            kept interpretation_candidate%ROWTYPE;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'interpretation_candidate % is never deleted (backlog E4-S7)',
                    OLD.id USING ERRCODE = '{_E}';
            END IF;
            kept := NEW;
            kept.eliminated_by := OLD.eliminated_by;
            IF kept IS DISTINCT FROM OLD
               OR (OLD.eliminated_by IS NOT NULL
                   AND NEW.eliminated_by IS DISTINCT FROM OLD.eliminated_by) THEN
                RAISE EXCEPTION 'interpretation_candidate %: only eliminated_by may be set, once '
                                '(backlog E4-S7)', OLD.id USING ERRCODE = '{_E}';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER interpretation_candidate_append_only BEFORE UPDATE OR DELETE "
        "ON interpretation_candidate "
        "FOR EACH ROW EXECUTE FUNCTION interpretation_candidate_append_only()"
    )

    # ------------------------------------------------------------------ verified_fact
    op.execute(
        """
        CREATE TABLE verified_fact (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            fact_id             uuid NOT NULL,
            document_id         uuid REFERENCES source_document (id),
            candidate_id        uuid NOT NULL REFERENCES interpretation_candidate (id),
            observation_ids     uuid[] NOT NULL DEFAULT '{}',
            fact_type           text NOT NULL,
            value_text          text,
            value_num           numeric,
            unit                text,
            code_system         text,
            code                text,
            review_state        text NOT NULL,
            verification_method text NOT NULL CHECK (verification_method IN
                                    ('auto_gate', 'reviewer')),
            confidence          real CHECK (confidence BETWEEN 0 AND 1),
            policy_id           text,
            reviewer_id         text,
            model_version       text,
            created_at          timestamptz NOT NULL DEFAULT now(),
            CHECK ((verification_method = 'reviewer') = (reviewer_id IS NOT NULL))
        )
        """
    )
    op.execute("CREATE INDEX ix_verified_fact_fact ON verified_fact (fact_id)")
    op.execute("CREATE INDEX ix_verified_fact_doc ON verified_fact (document_id)")
    op.execute(
        f"""
        CREATE FUNCTION verified_fact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'verified_fact % is never updated or deleted (backlog E4-S7)',
                OLD.id USING ERRCODE = '{_E}';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER verified_fact_immutable BEFORE UPDATE OR DELETE ON verified_fact "
        "FOR EACH ROW EXECUTE FUNCTION verified_fact_immutable()"
    )
    for table in ("ocr_observation", "interpretation_candidate", "verified_fact"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION evidence_no_truncate()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS verified_fact")
    op.execute("DROP TABLE IF EXISTS interpretation_candidate")
    op.execute("DROP FUNCTION IF EXISTS verified_fact_immutable()")
    op.execute("DROP FUNCTION IF EXISTS interpretation_candidate_append_only()")
    op.execute("DROP VIEW IF EXISTS ocr_block")
    op.execute("DROP TRIGGER IF EXISTS ocr_observation_no_truncate ON ocr_observation")
    op.execute("DROP TRIGGER IF EXISTS ocr_observation_append_only ON ocr_observation")
    op.execute("DROP FUNCTION IF EXISTS evidence_no_truncate()")
    op.execute("DROP FUNCTION IF EXISTS ocr_observation_append_only()")
    # superseded observations had been deleted before 0013: the old table holds current ones
    op.execute("DELETE FROM ocr_observation WHERE superseded_by IS NOT NULL")
    op.execute("DROP INDEX IF EXISTS ix_ocr_observation_current")
    op.execute(
        """
        ALTER TABLE ocr_observation
            DROP COLUMN crop_hash, DROP COLUMN region_kind, DROP COLUMN field_domain,
            DROP COLUMN engine_version, DROP COLUMN prompt_hash, DROP COLUMN token_confidences,
            DROP COLUMN superseded_by, DROP COLUMN superseded_at
        """
    )
    op.execute("ALTER INDEX ix_ocr_observation_fts RENAME TO ix_ocr_block_fts")
    op.execute("ALTER INDEX ix_ocr_observation_page RENAME TO ix_ocr_block_page")
    op.execute("ALTER TABLE ocr_observation RENAME COLUMN raw_text TO text")
    op.execute("ALTER TABLE ocr_observation RENAME TO ocr_block")
