-- CDI-Adapter per-document schema (ARCHITECTURE §4.1–§4.4). PostgreSQL 16, jsonb throughout.
-- Applied by Alembic revision 0001_initial_schema. 20 tables in 13 groups.
-- Later revisions: 0002 patient MPI, 0003 patient registry, 0004 canonical EMR (§4.5).

-- ------------------------------------------------------------------ Identity (MPI)
CREATE TABLE patient_identity (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    abha_number     text,
    abha_address    text,
    legacy_mrn      text,
    name            text,
    sex             text CHECK (sex IN ('M', 'F', 'O', 'U')),
    birth_date      date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_patient_identity_abha ON patient_identity (abha_number)
    WHERE abha_number IS NOT NULL;
CREATE INDEX ix_patient_identity_mrn ON patient_identity (legacy_mrn);

-- ------------------------------------------------------------------ Ingestion evidence (immutable)
CREATE TABLE source_document (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256              char(64) NOT NULL UNIQUE,
    filename            text NOT NULL,
    mime_type           text NOT NULL,
    byte_size           bigint NOT NULL CHECK (byte_size >= 0),
    storage_key         text NOT NULL,
    source_channel      text NOT NULL DEFAULT 'upload',
    legacy_ref          text,
    legacy_patient_ref  text,
    patient_id          uuid REFERENCES patient_identity (id),
    page_count          int,
    status              text NOT NULL DEFAULT 'received' CHECK (status IN (
                            'received', 'pages_rendered', 'ocr_done', 'classified', 'extracted',
                            'normalized', 'validated', 'projected', 'error')),
    error               jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_source_document_patient ON source_document (patient_id);

CREATE TABLE document_page (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid NOT NULL REFERENCES source_document (id) ON DELETE CASCADE,
    page_no         int NOT NULL CHECK (page_no >= 1),
    storage_key     text NOT NULL,
    thumb_key       text,
    width           int,
    height          int,
    dpi             int,
    preproc         jsonb NOT NULL DEFAULT '{}'::jsonb,      -- {skew_deg, steps[]}
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_no)
);

CREATE TABLE pipeline_run (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid REFERENCES source_document (id) ON DELETE CASCADE,
    stage           text NOT NULL CHECK (stage IN (
                        'ingest', 'ocr', 'classify', 'extract', 'terminology', 'validate', 'project')),
    status          text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
    model_name      text,
    model_version   text,
    metrics         jsonb NOT NULL DEFAULT '{}'::jsonb,
    error           text,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX ix_pipeline_run_document ON pipeline_run (document_id, stage);

-- ------------------------------------------------------------------ Classification
CREATE TABLE doc_classification (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid NOT NULL REFERENCES source_document (id) ON DELETE CASCADE,
    doc_type        text NOT NULL,
    specialty       text,
    languages       text[] NOT NULL DEFAULT '{}',
    is_handwritten  boolean NOT NULL DEFAULT false,
    page_spans      jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence      real CHECK (confidence BETWEEN 0 AND 1),
    method          text NOT NULL DEFAULT 'heuristic' CHECK (method IN ('heuristic', 'vlm')),
    raw             jsonb,
    pipeline_run_id uuid REFERENCES pipeline_run (id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_doc_classification_document ON doc_classification (document_id);

-- ------------------------------------------------------------------ OCR evidence
CREATE TABLE ocr_block (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid NOT NULL REFERENCES source_document (id) ON DELETE CASCADE,
    page_id         uuid NOT NULL REFERENCES document_page (id) ON DELETE CASCADE,
    block_type      text NOT NULL DEFAULT 'line' CHECK (block_type IN ('line', 'word', 'cell', 'page')),
    text            text NOT NULL,
    bbox            int[] NOT NULL CHECK (array_length(bbox, 1) = 4),   -- [x0,y0,x1,y1]
    polygon         jsonb,
    ocr_conf        real CHECK (ocr_conf BETWEEN 0 AND 1),
    reading_order   int NOT NULL,
    table_ref       jsonb,
    engine          text NOT NULL,                                     -- rapidocr | vlm
    pipeline_run_id uuid REFERENCES pipeline_run (id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ocr_block_page ON ocr_block (page_id, reading_order);
CREATE INDEX ix_ocr_block_fts ON ocr_block USING gin (to_tsvector('simple', text));

-- ------------------------------------------------------------------ Raw extraction
CREATE TABLE extraction (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid NOT NULL REFERENCES source_document (id) ON DELETE CASCADE,
    doc_type        text NOT NULL,
    schema_id       text NOT NULL,
    model_name      text,
    model_version   text,
    payload         jsonb NOT NULL,                  -- schema-locked VLM payload, pre-normalization
    evidence_map    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- bN -> ocr_block uuid
    is_partial      boolean NOT NULL DEFAULT false,
    pipeline_run_id uuid REFERENCES pipeline_run (id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_extraction_document ON extraction (document_id);

CREATE TABLE patient_identity_alias (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          uuid NOT NULL REFERENCES patient_identity (id) ON DELETE CASCADE,
    source_document_id  uuid REFERENCES source_document (id) ON DELETE CASCADE,
    name                text,
    sex                 text,
    birth_date          date,
    age_text            text,
    raw                 jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_patient_identity_alias_patient ON patient_identity_alias (patient_id);

-- ------------------------------------------------------------------ Encounters
CREATE TABLE encounter (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          uuid NOT NULL REFERENCES patient_identity (id),
    source_document_id  uuid REFERENCES source_document (id) ON DELETE CASCADE,
    class               text NOT NULL DEFAULT 'AMB' CHECK (class IN ('AMB', 'IMP')),
    period_start        date,
    date_precision      text CHECK (date_precision IN ('year', 'month', 'day')),
    doc_type            text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_encounter_patient ON encounter (patient_id, period_start);

-- ------------------------------------------------------------------ Clinical facts (EAV core, §4.2)
CREATE TABLE clinical_fact (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id              uuid NOT NULL REFERENCES patient_identity (id),
    encounter_id            uuid REFERENCES encounter (id) ON DELETE SET NULL,
    fact_type               text NOT NULL CHECK (fact_type IN (
                                'condition', 'symptom', 'finding', 'lab_result', 'vital_sign',
                                'procedure', 'medication', 'allergy', 'immunization',
                                'diagnostic_report', 'advice', 'care_plan', 'referral', 'document')),
    code_system             text,
    code                    text,
    code_display            text,
    code_status             text NOT NULL DEFAULT 'unmapped' CHECK (code_status IN (
                                'unmapped', 'candidate', 'bound', 'local_only')),
    local_text              text,
    value_kind              text CHECK (value_kind IN (
                                'quantity', 'codeable', 'text', 'boolean', 'range')),
    value_num               numeric,
    value_unit_ucum         text,
    value_code_system       text,
    value_code              text,
    value_code_display      text,
    value_text              text,
    value_bool              boolean,
    value_low_num           numeric,
    value_high_num          numeric,
    ref_range_low           numeric,
    ref_range_high          numeric,
    ref_range_text          text,
    abnormal_flag           text,
    clinical_status         text,
    verification            text,
    onset                   timestamptz,
    effective_time          timestamptz,
    asserted_time           timestamptz,
    extraction_id           uuid REFERENCES extraction (id) ON DELETE SET NULL,
    source_doc_ids          uuid[] NOT NULL DEFAULT '{}',
    confidence_overall      real CHECK (confidence_overall BETWEEN 0 AND 1),
    confidence_ocr          real CHECK (confidence_ocr BETWEEN 0 AND 1),
    confidence_extract      real CHECK (confidence_extract BETWEEN 0 AND 1),
    confidence_terminology  real CHECK (confidence_terminology BETWEEN 0 AND 1),
    review_state            text NOT NULL DEFAULT 'pending' CHECK (review_state IN (
                                'pending', 'auto_accepted', 'in_review',
                                'clinician_confirmed', 'corrected', 'rejected')),
    review_note             jsonb,
    supersedes              uuid REFERENCES clinical_fact (id),
    is_current              boolean NOT NULL DEFAULT true,
    dedup_key               text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_clinical_fact_patient ON clinical_fact (patient_id, fact_type, code);
CREATE INDEX ix_clinical_fact_review ON clinical_fact (review_state);
CREATE INDEX ix_clinical_fact_source ON clinical_fact USING gin (source_doc_ids);

CREATE TABLE medication_detail (
    fact_id             uuid PRIMARY KEY REFERENCES clinical_fact (id) ON DELETE CASCADE,
    drug_text           text,
    rxlike_system       text,
    rxlike_code         text,
    form                text,
    strength_num        numeric,
    strength_unit       text,
    dose_num            numeric,
    dose_unit_ucum      text,
    route               text,
    frequency_code      text,
    frequency_per_day   numeric,
    duration_days       numeric,
    prn                 boolean NOT NULL DEFAULT false,
    instructions        text,
    intent              text NOT NULL DEFAULT 'order' CHECK (intent IN ('order', 'record'))
);

-- ------------------------------------------------------------------ Provenance (first-class, §4.3)
CREATE TABLE fact_provenance (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id             uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
    source_doc_id       uuid REFERENCES source_document (id) ON DELETE CASCADE,
    page_id             uuid REFERENCES document_page (id) ON DELETE SET NULL,
    ocr_block_ids       uuid[] NOT NULL DEFAULT '{}',
    bbox_union          int[],
    extracted_text      text,
    pipeline_run_ids    uuid[] NOT NULL DEFAULT '{}',
    model_stack         jsonb NOT NULL DEFAULT '{}'::jsonb,
    agent               text NOT NULL DEFAULT 'system',      -- 'system' | <clinician id>
    recorded_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_fact_provenance_fact ON fact_provenance (fact_id);

-- ------------------------------------------------------------------ Conflicts
CREATE TABLE fact_conflict (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          uuid NOT NULL REFERENCES patient_identity (id),
    fact_a              uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
    fact_b              uuid NOT NULL REFERENCES clinical_fact (id) ON DELETE CASCADE,
    conflict_type       text NOT NULL,                        -- value_mismatch | duplicate | ...
    evidence_state      text NOT NULL CHECK (evidence_state IN (
                            'SUPPORTED', 'CONTRADICTED', 'UNKNOWN_NOT_MENTIONED')),
    severity            text NOT NULL CHECK (severity IN ('blocker', 'warn', 'info')),
    auto_resolution     text,
    resolved            boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ FHIR projection (§4.4)
CREATE TABLE fhir_resource (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_type       text NOT NULL,
    fhir_id             text NOT NULL,
    version_id          int NOT NULL DEFAULT 1,
    patient_id          uuid REFERENCES patient_identity (id),
    encounter_id        uuid REFERENCES encounter (id) ON DELETE SET NULL,
    source_document_id  uuid REFERENCES source_document (id) ON DELETE CASCADE,
    profile             text[] NOT NULL DEFAULT '{}',
    resource            jsonb NOT NULL,
    derived_from_facts  uuid[] NOT NULL DEFAULT '{}',
    validation_status   text NOT NULL DEFAULT 'pending' CHECK (validation_status IN (
                            'pending', 'valid', 'warning', 'error')),
    validation_issues   jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (resource_type, fhir_id, version_id)
);

CREATE TABLE fhir_bundle (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          uuid REFERENCES patient_identity (id),
    encounter_id        uuid REFERENCES encounter (id) ON DELETE SET NULL,
    source_document_id  uuid REFERENCES source_document (id) ON DELETE CASCADE,
    care_context        text,
    artifact_type       text NOT NULL,
    bundle              jsonb NOT NULL,
    bundle_hash         char(64) NOT NULL,
    fhir_version        text NOT NULL DEFAULT '4.0.1',
    ig_package          text NOT NULL DEFAULT 'nrces.fhir.r4.ndhm#6.5.0',
    validation_status   text NOT NULL DEFAULT 'pending' CHECK (validation_status IN (
                            'pending', 'valid', 'warning', 'error')),
    status              text NOT NULL DEFAULT 'draft' CHECK (status IN (
                            'draft', 'validated', 'ready_to_share', 'shared', 'superseded')),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_fhir_bundle_patient ON fhir_bundle (patient_id);

-- ------------------------------------------------------------------ Human review
CREATE TABLE review_task (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id         uuid REFERENCES source_document (id) ON DELETE CASCADE,
    patient_id          uuid REFERENCES patient_identity (id),
    kind                text NOT NULL,          -- dose_check | unmapped_terminology | low_confidence | audit_sample
    priority            text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high')),
    status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    fact_ids            uuid[] NOT NULL DEFAULT '{}',
    payload             jsonb NOT NULL DEFAULT '{}'::jsonb,
    assigned_to         text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    closed_at           timestamptz
);
CREATE INDEX ix_review_task_open ON review_task (status, created_at);

-- ------------------------------------------------------------------ ABDM (schema only, §13)
CREATE TABLE abdm_care_context (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      uuid NOT NULL REFERENCES patient_identity (id),
    reference       text NOT NULL UNIQUE,
    display         text,
    linked_at       timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE abdm_consent (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consent_id      text NOT NULL UNIQUE,
    patient_id      uuid REFERENCES patient_identity (id),
    care_context_id uuid REFERENCES abdm_care_context (id),
    status          text NOT NULL DEFAULT 'requested',
    artifact        jsonb,
    expires_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE abdm_transfer (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consent_id      uuid REFERENCES abdm_consent (id),
    bundle_id       uuid REFERENCES fhir_bundle (id),
    status          text NOT NULL DEFAULT 'pending',
    attempts        int NOT NULL DEFAULT 0,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ Audit
CREATE TABLE audit_log (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    actor           text NOT NULL,
    action          text NOT NULL CHECK (action IN ('create', 'update', 'read', 'delete')),
    entity          text NOT NULL,
    entity_id       text,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_audit_log_entity ON audit_log (entity, entity_id);
