"""Medicine catalogue (owner request 2026-09-26: the Tata 1mg catalogue as the medicine master).

A normalisation layer AFTER extraction and terminology (S5), never a replacement: the model's
``drug_text`` / ``local_text`` stay exactly as read; the catalogue's identification of that text is
stored beside the fact.

* ``med_catalogue_import`` — provenance of every catalogue load: source, file name, sha256, rows.
* ``med_catalogue`` — one row per catalogue product. ``brand_norm`` is the name reduced to its
  brand words (no dosage form, no strength), the key the matcher searches with ``pg_trgm``.
* ``medication_catalogue_match`` — per medication fact: the matched product, or
  ``status = 'unknown'`` (UNKNOWN_MEDICATION) when no match is reliable; ``candidates`` keeps the
  closest products so a reviewer can pick one. Deleted with its fact.

``pg_trgm`` ships with PostgreSQL (``postgresql-16`` contrib) and is a trusted extension, so the
app role can create it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE TABLE med_catalogue_import (
            id              bigserial PRIMARY KEY,
            source          text NOT NULL,
            file_name       text NOT NULL,
            sha256          char(64) NOT NULL,
            rows_loaded     integer NOT NULL,
            imported_by     text NOT NULL,
            imported_at     timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """
        CREATE TABLE med_catalogue (
            id              bigserial PRIMARY KEY,
            source          text NOT NULL,
            source_id       text NOT NULL,
            import_id       bigint NOT NULL REFERENCES med_catalogue_import (id),
            name            text NOT NULL,
            brand_norm      text NOT NULL,
            strengths       numeric[] NOT NULL DEFAULT '{}',
            manufacturer    text,
            dosage_form     text,
            pack_size_label text,
            composition     text,
            price           numeric,
            discontinued    boolean NOT NULL DEFAULT false,
            extra           jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (source, source_id)
        )"""
    )
    op.execute("CREATE INDEX ix_med_catalogue_brand ON med_catalogue (brand_norm)")
    op.execute("CREATE INDEX ix_med_catalogue_brand_trgm ON med_catalogue "
               "USING gin (brand_norm gin_trgm_ops)")  # fmt: skip
    op.execute(
        """
        CREATE TABLE medication_catalogue_match (
            fact_id         uuid PRIMARY KEY REFERENCES clinical_fact (id) ON DELETE CASCADE,
            status          text NOT NULL CHECK (status IN ('matched', 'unknown')),
            catalogue_id    bigint REFERENCES med_catalogue (id) ON DELETE SET NULL,
            display_name    text NOT NULL,
            score           real,
            method          text,
            strength_match  boolean,
            query           text,
            candidates      jsonb NOT NULL DEFAULT '[]'::jsonb,
            import_id       bigint REFERENCES med_catalogue_import (id),
            matched_at      timestamptz NOT NULL DEFAULT now()
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE medication_catalogue_match")
    op.execute("DROP TABLE med_catalogue")
    op.execute("DROP TABLE med_catalogue_import")
