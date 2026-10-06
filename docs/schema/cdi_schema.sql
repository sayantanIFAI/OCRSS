--
-- PostgreSQL database dump
--

\restrict ViWrrf2532ZUXp0kc205ky2pG21ywDox1oIgVLz8eAkyY0Ak40Jq1WNNDAA9gll

-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: abdm_care_context; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.abdm_care_context (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    reference text NOT NULL,
    display text,
    linked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: abdm_consent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.abdm_consent (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    consent_id text NOT NULL,
    patient_id uuid,
    care_context_id uuid,
    status text DEFAULT 'requested'::text NOT NULL,
    artifact jsonb,
    expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: abdm_transfer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.abdm_transfer (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    consent_id uuid,
    bundle_id uuid,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_clinical_verification; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_clinical_verification (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    ai_suggestion_id uuid NOT NULL,
    reviewer text NOT NULL,
    decision text NOT NULL,
    final_value jsonb,
    decided_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_clinical_verification_decision_check CHECK ((decision = ANY (ARRAY['accepted'::text, 'corrected'::text, 'rejected'::text])))
);


--
-- Name: ai_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rv_item_id uuid,
    source_document_id uuid,
    kind text NOT NULL,
    model_stack jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ai_suggestion; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_suggestion (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    ai_event_id uuid NOT NULL,
    fact_id uuid,
    element_type text NOT NULL,
    suggested jsonb NOT NULL,
    confidence real,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_suggestion_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    entity text NOT NULL,
    entity_id text,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT audit_log_action_check CHECK ((action = ANY (ARRAY['create'::text, 'update'::text, 'read'::text, 'delete'::text])))
);


--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;


--
-- Name: clinical_fact; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clinical_fact (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    encounter_id uuid,
    fact_type text NOT NULL,
    code_system text,
    code text,
    code_display text,
    code_status text DEFAULT 'unmapped'::text NOT NULL,
    local_text text,
    value_kind text,
    value_num numeric,
    value_unit_ucum text,
    value_code_system text,
    value_code text,
    value_code_display text,
    value_text text,
    value_bool boolean,
    value_low_num numeric,
    value_high_num numeric,
    ref_range_low numeric,
    ref_range_high numeric,
    ref_range_text text,
    abnormal_flag text,
    clinical_status text,
    verification text,
    onset timestamp with time zone,
    effective_time timestamp with time zone,
    asserted_time timestamp with time zone,
    extraction_id uuid,
    source_doc_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    confidence_overall real,
    confidence_ocr real,
    confidence_extract real,
    confidence_terminology real,
    review_state text DEFAULT 'pending'::text NOT NULL,
    review_note jsonb,
    supersedes uuid,
    is_current boolean DEFAULT true NOT NULL,
    dedup_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT clinical_fact_code_status_check CHECK ((code_status = ANY (ARRAY['unmapped'::text, 'candidate'::text, 'bound'::text, 'local_only'::text]))),
    CONSTRAINT clinical_fact_confidence_extract_check CHECK (((confidence_extract >= (0)::double precision) AND (confidence_extract <= (1)::double precision))),
    CONSTRAINT clinical_fact_confidence_ocr_check CHECK (((confidence_ocr >= (0)::double precision) AND (confidence_ocr <= (1)::double precision))),
    CONSTRAINT clinical_fact_confidence_overall_check CHECK (((confidence_overall >= (0)::double precision) AND (confidence_overall <= (1)::double precision))),
    CONSTRAINT clinical_fact_confidence_terminology_check CHECK (((confidence_terminology >= (0)::double precision) AND (confidence_terminology <= (1)::double precision))),
    CONSTRAINT clinical_fact_fact_type_check CHECK ((fact_type = ANY (ARRAY['condition'::text, 'symptom'::text, 'finding'::text, 'lab_result'::text, 'vital_sign'::text, 'procedure'::text, 'medication'::text, 'allergy'::text, 'immunization'::text, 'diagnostic_report'::text, 'advice'::text, 'care_plan'::text, 'referral'::text, 'document'::text]))),
    CONSTRAINT clinical_fact_review_state_check CHECK ((review_state = ANY (ARRAY['pending'::text, 'auto_accepted'::text, 'in_review'::text, 'clinician_confirmed'::text, 'corrected'::text, 'rejected'::text]))),
    CONSTRAINT clinical_fact_value_kind_check CHECK ((value_kind = ANY (ARRAY['quantity'::text, 'codeable'::text, 'text'::text, 'boolean'::text, 'range'::text])))
);


--
-- Name: cn_allergy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cn_allergy (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    rv_item_id uuid,
    source_fact_id uuid,
    encounter_id uuid,
    review_state text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    code_system text,
    code text,
    substance text NOT NULL,
    reaction text,
    clinical_status text
);


--
-- Name: cn_clinical_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cn_clinical_note (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    encounter_id uuid,
    note_type text NOT NULL,
    body text NOT NULL,
    author text,
    source text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: cn_condition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cn_condition (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    rv_item_id uuid,
    source_fact_id uuid,
    encounter_id uuid,
    review_state text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    code_system text,
    code text,
    display text NOT NULL,
    clinical_status text,
    verification text,
    onset timestamp with time zone
);


--
-- Name: cn_fhir_bundle; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cn_fhir_bundle (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    rv_item_id uuid,
    artifact_type text NOT NULL,
    bundle jsonb NOT NULL,
    bundle_hash character(64) NOT NULL,
    validation jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cn_fhir_bundle_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'validated'::text, 'ready_to_share'::text, 'shared'::text, 'superseded'::text])))
);


--
-- Name: cn_lab_result; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cn_lab_result (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    rv_item_id uuid,
    source_fact_id uuid,
    encounter_id uuid,
    review_state text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    test_system text,
    test_code text,
    test_display text NOT NULL,
    value_num numeric,
    value_text text,
    unit_ucum text,
    ref_range_low numeric,
    ref_range_high numeric,
    ref_range_text text,
    abnormal_flag text,
    effective_time timestamp with time zone
);


--
-- Name: cn_medication; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cn_medication (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    rv_item_id uuid,
    source_fact_id uuid,
    encounter_id uuid,
    review_state text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    code_system text,
    code text,
    drug_text text NOT NULL,
    strength_num numeric,
    strength_unit text,
    dose_num numeric,
    dose_unit_ucum text,
    route text,
    frequency_code text,
    frequency_per_day numeric,
    duration_days numeric,
    instructions text,
    status text DEFAULT 'active'::text NOT NULL
);


--
-- Name: cn_vital_sign; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cn_vital_sign (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    rv_item_id uuid,
    source_fact_id uuid,
    encounter_id uuid,
    review_state text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    code_system text,
    code text,
    display text NOT NULL,
    value_num numeric,
    value_text text,
    unit_ucum text,
    effective_time timestamp with time zone
);


--
-- Name: doc_classification; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.doc_classification (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    doc_type text NOT NULL,
    specialty text,
    languages text[] DEFAULT '{}'::text[] NOT NULL,
    is_handwritten boolean DEFAULT false NOT NULL,
    page_spans jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence real,
    method text DEFAULT 'heuristic'::text NOT NULL,
    raw jsonb,
    pipeline_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT doc_classification_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))),
    CONSTRAINT doc_classification_method_check CHECK ((method = ANY (ARRAY['heuristic'::text, 'vlm'::text])))
);


--
-- Name: document_page; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_page (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    page_no integer NOT NULL,
    storage_key text NOT NULL,
    thumb_key text,
    width integer,
    height integer,
    dpi integer,
    preproc jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT document_page_page_no_check CHECK ((page_no >= 1))
);


--
-- Name: encounter; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.encounter (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    source_document_id uuid,
    class text DEFAULT 'AMB'::text NOT NULL,
    period_start date,
    date_precision text,
    doc_type text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT encounter_class_check CHECK ((class = ANY (ARRAY['AMB'::text, 'IMP'::text]))),
    CONSTRAINT encounter_date_precision_check CHECK ((date_precision = ANY (ARRAY['year'::text, 'month'::text, 'day'::text])))
);


--
-- Name: extraction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extraction (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    doc_type text NOT NULL,
    schema_id text NOT NULL,
    model_name text,
    model_version text,
    payload jsonb NOT NULL,
    evidence_map jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_partial boolean DEFAULT false NOT NULL,
    pipeline_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fact_conflict; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_conflict (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    fact_a uuid NOT NULL,
    fact_b uuid NOT NULL,
    conflict_type text NOT NULL,
    evidence_state text NOT NULL,
    severity text NOT NULL,
    auto_resolution text,
    resolved boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fact_conflict_evidence_state_check CHECK ((evidence_state = ANY (ARRAY['SUPPORTED'::text, 'CONTRADICTED'::text, 'UNKNOWN_NOT_MENTIONED'::text]))),
    CONSTRAINT fact_conflict_severity_check CHECK ((severity = ANY (ARRAY['blocker'::text, 'warn'::text, 'info'::text])))
);


--
-- Name: fact_provenance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fact_provenance (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fact_id uuid NOT NULL,
    source_doc_id uuid,
    page_id uuid,
    ocr_block_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    bbox_union integer[],
    extracted_text text,
    pipeline_run_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    model_stack jsonb DEFAULT '{}'::jsonb NOT NULL,
    agent text DEFAULT 'system'::text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fhir_bundle; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fhir_bundle (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid,
    encounter_id uuid,
    source_document_id uuid,
    care_context text,
    artifact_type text NOT NULL,
    bundle jsonb NOT NULL,
    bundle_hash character(64) NOT NULL,
    fhir_version text DEFAULT '4.0.1'::text NOT NULL,
    ig_package text DEFAULT 'nrces.fhir.r4.ndhm#6.5.0'::text NOT NULL,
    validation_status text DEFAULT 'pending'::text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fhir_bundle_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'validated'::text, 'ready_to_share'::text, 'shared'::text, 'superseded'::text]))),
    CONSTRAINT fhir_bundle_validation_status_check CHECK ((validation_status = ANY (ARRAY['pending'::text, 'valid'::text, 'warning'::text, 'error'::text])))
);


--
-- Name: fhir_resource; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fhir_resource (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    resource_type text NOT NULL,
    fhir_id text NOT NULL,
    version_id integer DEFAULT 1 NOT NULL,
    patient_id uuid,
    encounter_id uuid,
    source_document_id uuid,
    profile text[] DEFAULT '{}'::text[] NOT NULL,
    resource jsonb NOT NULL,
    derived_from_facts uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    validation_status text DEFAULT 'pending'::text NOT NULL,
    validation_issues jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fhir_resource_validation_status_check CHECK ((validation_status = ANY (ARRAY['pending'::text, 'valid'::text, 'warning'::text, 'error'::text])))
);


--
-- Name: fin_billing_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fin_billing_item (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    visit_id uuid,
    source_cn_table text,
    source_cn_id uuid,
    description text NOT NULL,
    amount numeric(12,2),
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: medication_detail; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.medication_detail (
    fact_id uuid NOT NULL,
    drug_text text,
    rxlike_system text,
    rxlike_code text,
    form text,
    strength_num numeric,
    strength_unit text,
    dose_num numeric,
    dose_unit_ucum text,
    route text,
    frequency_code text,
    frequency_per_day numeric,
    duration_days numeric,
    prn boolean DEFAULT false NOT NULL,
    instructions text,
    intent text DEFAULT 'order'::text NOT NULL,
    CONSTRAINT medication_detail_intent_check CHECK ((intent = ANY (ARRAY['order'::text, 'record'::text])))
);


--
-- Name: ocr_block; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ocr_block (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid NOT NULL,
    page_id uuid NOT NULL,
    block_type text DEFAULT 'line'::text NOT NULL,
    text text NOT NULL,
    bbox integer[] NOT NULL,
    polygon jsonb,
    ocr_conf real,
    reading_order integer NOT NULL,
    table_ref jsonb,
    engine text NOT NULL,
    pipeline_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ocr_block_bbox_check CHECK ((array_length(bbox, 1) = 4)),
    CONSTRAINT ocr_block_block_type_check CHECK ((block_type = ANY (ARRAY['line'::text, 'word'::text, 'cell'::text, 'page'::text]))),
    CONSTRAINT ocr_block_ocr_conf_check CHECK (((ocr_conf >= (0)::double precision) AND (ocr_conf <= (1)::double precision)))
);


--
-- Name: ops_patient_visit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ops_patient_visit (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    visit_date date DEFAULT CURRENT_DATE NOT NULL,
    counter text,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: patient_identity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patient_identity (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    abha_number text,
    abha_address text,
    legacy_mrn text,
    name text,
    sex text,
    birth_date date,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    mpi_id text,
    identity_confidence real,
    CONSTRAINT patient_identity_identity_confidence_check CHECK (((identity_confidence >= (0)::double precision) AND (identity_confidence <= (1)::double precision))),
    CONSTRAINT patient_identity_sex_check CHECK ((sex = ANY (ARRAY['M'::text, 'F'::text, 'O'::text, 'U'::text])))
);


--
-- Name: patient_identity_alias; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patient_identity_alias (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    source_document_id uuid,
    name text,
    sex text,
    birth_date date,
    age_text text,
    raw jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: patient_mpi_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.patient_mpi_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: patient_registry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.patient_registry (
    patient_id text NOT NULL,
    name text NOT NULL,
    mobile text NOT NULL,
    dob date NOT NULL,
    gender text,
    address text,
    abha_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT patient_registry_gender_check CHECK ((gender = ANY (ARRAY['M'::text, 'F'::text, 'O'::text, 'U'::text])))
);


--
-- Name: pipeline_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipeline_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid,
    stage text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    model_name text,
    model_version text,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT pipeline_run_stage_check CHECK ((stage = ANY (ARRAY['ingest'::text, 'ocr'::text, 'classify'::text, 'extract'::text, 'terminology'::text, 'validate'::text, 'project'::text]))),
    CONSTRAINT pipeline_run_status_check CHECK ((status = ANY (ARRAY['running'::text, 'ok'::text, 'failed'::text])))
);


--
-- Name: review_task; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.review_task (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    document_id uuid,
    patient_id uuid,
    kind text NOT NULL,
    priority text DEFAULT 'normal'::text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    fact_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    assigned_to text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    CONSTRAINT review_task_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'normal'::text, 'high'::text]))),
    CONSTRAINT review_task_status_check CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text])))
);


--
-- Name: rv_action; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rv_action (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rv_item_id uuid NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rv_action_action_check CHECK ((action = ANY (ARRAY['create'::text, 'claim'::text, 'release'::text, 'accept'::text, 'correct'::text, 'reject'::text, 'approve'::text, 'promote'::text])))
);


--
-- Name: rv_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rv_item (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    patient_id uuid NOT NULL,
    source_document_id uuid,
    job_id text,
    doc_type text,
    status text DEFAULT 'workbasket'::text NOT NULL,
    assigned_to text,
    claimed_at timestamp with time zone,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rv_item_status_check CHECK ((status = ANY (ARRAY['workbasket'::text, 'claimed'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: rv_item_element; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rv_item_element (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rv_item_id uuid NOT NULL,
    fact_id uuid,
    element_type text NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    state text DEFAULT 'held'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rv_item_element_state_check CHECK ((state = ANY (ARRAY['held'::text, 'accepted'::text, 'corrected'::text, 'rejected'::text])))
);


--
-- Name: source_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source_document (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sha256 character(64) NOT NULL,
    filename text NOT NULL,
    mime_type text NOT NULL,
    byte_size bigint NOT NULL,
    storage_key text NOT NULL,
    source_channel text DEFAULT 'upload'::text NOT NULL,
    legacy_ref text,
    legacy_patient_ref text,
    patient_id uuid,
    page_count integer,
    status text DEFAULT 'received'::text NOT NULL,
    error jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT source_document_byte_size_check CHECK ((byte_size >= 0)),
    CONSTRAINT source_document_status_check CHECK ((status = ANY (ARRAY['received'::text, 'pages_rendered'::text, 'ocr_done'::text, 'classified'::text, 'extracted'::text, 'normalized'::text, 'validated'::text, 'projected'::text, 'error'::text])))
);


--
-- Name: v_allergy_list; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_allergy_list AS
 SELECT patient_id,
    id AS fact_id,
    code_system,
    code,
    COALESCE(code_display, local_text) AS substance,
    value_text AS reaction,
    clinical_status,
    review_state,
    encounter_id
   FROM public.clinical_fact f
  WHERE ((fact_type = 'allergy'::text) AND is_current);


--
-- Name: v_encounter_timeline; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_encounter_timeline AS
 SELECT e.patient_id,
    e.id AS encounter_id,
    e.period_start,
    e.date_precision,
    e.class,
    e.doc_type,
    e.source_document_id,
    d.filename,
    count(f.id) AS fact_count,
    count(f.id) FILTER (WHERE (f.review_state = ANY (ARRAY['auto_accepted'::text, 'clinician_confirmed'::text, 'corrected'::text]))) AS governed_count,
    count(f.id) FILTER (WHERE (f.review_state = ANY (ARRAY['pending'::text, 'in_review'::text]))) AS held_count
   FROM ((public.encounter e
     LEFT JOIN public.source_document d ON ((d.id = e.source_document_id)))
     LEFT JOIN public.clinical_fact f ON (((f.encounter_id = e.id) AND f.is_current)))
  GROUP BY e.patient_id, e.id, e.period_start, e.date_precision, e.class, e.doc_type, e.source_document_id, d.filename;


--
-- Name: v_medication_list; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_medication_list AS
 SELECT f.patient_id,
    f.id AS fact_id,
    f.code_system,
    f.code,
    COALESCE(f.code_display, m.drug_text, f.local_text) AS medication,
    m.strength_num,
    m.strength_unit,
    m.dose_num,
    m.dose_unit_ucum,
    m.route,
    m.frequency_code,
    m.frequency_per_day,
    m.duration_days,
    m.prn,
    m.instructions,
    f.clinical_status,
    f.review_state,
    f.encounter_id,
    e.period_start AS encounter_date
   FROM ((public.clinical_fact f
     LEFT JOIN public.medication_detail m ON ((m.fact_id = f.id)))
     LEFT JOIN public.encounter e ON ((e.id = f.encounter_id)))
  WHERE ((f.fact_type = 'medication'::text) AND f.is_current);


--
-- Name: v_problem_list; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_problem_list AS
 SELECT f.patient_id,
    f.id AS fact_id,
    f.code_system,
    f.code,
    COALESCE(f.code_display, f.local_text) AS problem,
    f.clinical_status,
    f.verification,
    f.onset,
    f.review_state,
    f.encounter_id,
    e.period_start AS encounter_date
   FROM (public.clinical_fact f
     LEFT JOIN public.encounter e ON ((e.id = f.encounter_id)))
  WHERE ((f.fact_type = 'condition'::text) AND f.is_current);


--
-- Name: v_results_grid; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_results_grid AS
 SELECT f.patient_id,
    f.id AS fact_id,
    f.fact_type,
    f.code_system,
    f.code,
    COALESCE(f.code_display, f.local_text) AS test,
    f.value_num,
    f.value_unit_ucum,
    f.value_text,
    f.ref_range_low,
    f.ref_range_high,
    f.ref_range_text,
    f.abnormal_flag,
    COALESCE(f.effective_time, (e.period_start)::timestamp with time zone) AS effective_time,
    f.review_state,
    f.encounter_id
   FROM (public.clinical_fact f
     LEFT JOIN public.encounter e ON ((e.id = f.encounter_id)))
  WHERE ((f.fact_type = ANY (ARRAY['lab_result'::text, 'vital_sign'::text])) AND f.is_current);


--
-- Name: audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);


--
-- Name: abdm_care_context abdm_care_context_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_care_context
    ADD CONSTRAINT abdm_care_context_pkey PRIMARY KEY (id);


--
-- Name: abdm_care_context abdm_care_context_reference_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_care_context
    ADD CONSTRAINT abdm_care_context_reference_key UNIQUE (reference);


--
-- Name: abdm_consent abdm_consent_consent_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_consent
    ADD CONSTRAINT abdm_consent_consent_id_key UNIQUE (consent_id);


--
-- Name: abdm_consent abdm_consent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_consent
    ADD CONSTRAINT abdm_consent_pkey PRIMARY KEY (id);


--
-- Name: abdm_transfer abdm_transfer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_transfer
    ADD CONSTRAINT abdm_transfer_pkey PRIMARY KEY (id);


--
-- Name: ai_clinical_verification ai_clinical_verification_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_clinical_verification
    ADD CONSTRAINT ai_clinical_verification_pkey PRIMARY KEY (id);


--
-- Name: ai_event ai_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_event
    ADD CONSTRAINT ai_event_pkey PRIMARY KEY (id);


--
-- Name: ai_suggestion ai_suggestion_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_suggestion
    ADD CONSTRAINT ai_suggestion_pkey PRIMARY KEY (id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: clinical_fact clinical_fact_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_fact
    ADD CONSTRAINT clinical_fact_pkey PRIMARY KEY (id);


--
-- Name: cn_allergy cn_allergy_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_allergy
    ADD CONSTRAINT cn_allergy_pkey PRIMARY KEY (id);


--
-- Name: cn_clinical_note cn_clinical_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_clinical_note
    ADD CONSTRAINT cn_clinical_note_pkey PRIMARY KEY (id);


--
-- Name: cn_condition cn_condition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_condition
    ADD CONSTRAINT cn_condition_pkey PRIMARY KEY (id);


--
-- Name: cn_fhir_bundle cn_fhir_bundle_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_fhir_bundle
    ADD CONSTRAINT cn_fhir_bundle_pkey PRIMARY KEY (id);


--
-- Name: cn_lab_result cn_lab_result_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_lab_result
    ADD CONSTRAINT cn_lab_result_pkey PRIMARY KEY (id);


--
-- Name: cn_medication cn_medication_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_medication
    ADD CONSTRAINT cn_medication_pkey PRIMARY KEY (id);


--
-- Name: cn_vital_sign cn_vital_sign_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_vital_sign
    ADD CONSTRAINT cn_vital_sign_pkey PRIMARY KEY (id);


--
-- Name: doc_classification doc_classification_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.doc_classification
    ADD CONSTRAINT doc_classification_pkey PRIMARY KEY (id);


--
-- Name: document_page document_page_document_id_page_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_page
    ADD CONSTRAINT document_page_document_id_page_no_key UNIQUE (document_id, page_no);


--
-- Name: document_page document_page_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_page
    ADD CONSTRAINT document_page_pkey PRIMARY KEY (id);


--
-- Name: encounter encounter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.encounter
    ADD CONSTRAINT encounter_pkey PRIMARY KEY (id);


--
-- Name: extraction extraction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction
    ADD CONSTRAINT extraction_pkey PRIMARY KEY (id);


--
-- Name: fact_conflict fact_conflict_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_conflict
    ADD CONSTRAINT fact_conflict_pkey PRIMARY KEY (id);


--
-- Name: fact_provenance fact_provenance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_provenance
    ADD CONSTRAINT fact_provenance_pkey PRIMARY KEY (id);


--
-- Name: fhir_bundle fhir_bundle_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_bundle
    ADD CONSTRAINT fhir_bundle_pkey PRIMARY KEY (id);


--
-- Name: fhir_resource fhir_resource_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_resource
    ADD CONSTRAINT fhir_resource_pkey PRIMARY KEY (id);


--
-- Name: fhir_resource fhir_resource_resource_type_fhir_id_version_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_resource
    ADD CONSTRAINT fhir_resource_resource_type_fhir_id_version_id_key UNIQUE (resource_type, fhir_id, version_id);


--
-- Name: fin_billing_item fin_billing_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fin_billing_item
    ADD CONSTRAINT fin_billing_item_pkey PRIMARY KEY (id);


--
-- Name: medication_detail medication_detail_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.medication_detail
    ADD CONSTRAINT medication_detail_pkey PRIMARY KEY (fact_id);


--
-- Name: ocr_block ocr_block_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ocr_block
    ADD CONSTRAINT ocr_block_pkey PRIMARY KEY (id);


--
-- Name: ops_patient_visit ops_patient_visit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ops_patient_visit
    ADD CONSTRAINT ops_patient_visit_pkey PRIMARY KEY (id);


--
-- Name: patient_identity_alias patient_identity_alias_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_identity_alias
    ADD CONSTRAINT patient_identity_alias_pkey PRIMARY KEY (id);


--
-- Name: patient_identity patient_identity_mpi_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_identity
    ADD CONSTRAINT patient_identity_mpi_id_key UNIQUE (mpi_id);


--
-- Name: patient_identity patient_identity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_identity
    ADD CONSTRAINT patient_identity_pkey PRIMARY KEY (id);


--
-- Name: patient_registry patient_registry_abha_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_registry
    ADD CONSTRAINT patient_registry_abha_id_key UNIQUE (abha_id);


--
-- Name: patient_registry patient_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_registry
    ADD CONSTRAINT patient_registry_pkey PRIMARY KEY (patient_id);


--
-- Name: pipeline_run pipeline_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_run
    ADD CONSTRAINT pipeline_run_pkey PRIMARY KEY (id);


--
-- Name: review_task review_task_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_task
    ADD CONSTRAINT review_task_pkey PRIMARY KEY (id);


--
-- Name: rv_action rv_action_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_action
    ADD CONSTRAINT rv_action_pkey PRIMARY KEY (id);


--
-- Name: rv_item_element rv_item_element_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_item_element
    ADD CONSTRAINT rv_item_element_pkey PRIMARY KEY (id);


--
-- Name: rv_item rv_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_item
    ADD CONSTRAINT rv_item_pkey PRIMARY KEY (id);


--
-- Name: source_document source_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_document
    ADD CONSTRAINT source_document_pkey PRIMARY KEY (id);


--
-- Name: source_document source_document_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_document
    ADD CONSTRAINT source_document_sha256_key UNIQUE (sha256);


--
-- Name: ix_audit_log_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_log_entity ON public.audit_log USING btree (entity, entity_id);


--
-- Name: ix_clinical_fact_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_fact_patient ON public.clinical_fact USING btree (patient_id, fact_type, code);


--
-- Name: ix_clinical_fact_review; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_fact_review ON public.clinical_fact USING btree (review_state);


--
-- Name: ix_clinical_fact_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_clinical_fact_source ON public.clinical_fact USING gin (source_doc_ids);


--
-- Name: ix_cn_allergy_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_allergy_patient ON public.cn_allergy USING btree (patient_id);


--
-- Name: ix_cn_condition_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_condition_patient ON public.cn_condition USING btree (patient_id);


--
-- Name: ix_cn_lab_result_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_lab_result_patient ON public.cn_lab_result USING btree (patient_id);


--
-- Name: ix_cn_medication_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_medication_patient ON public.cn_medication USING btree (patient_id);


--
-- Name: ix_cn_vital_sign_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cn_vital_sign_patient ON public.cn_vital_sign USING btree (patient_id);


--
-- Name: ix_doc_classification_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_doc_classification_document ON public.doc_classification USING btree (document_id);


--
-- Name: ix_encounter_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_encounter_patient ON public.encounter USING btree (patient_id, period_start);


--
-- Name: ix_extraction_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_extraction_document ON public.extraction USING btree (document_id);


--
-- Name: ix_fact_provenance_fact; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fact_provenance_fact ON public.fact_provenance USING btree (fact_id);


--
-- Name: ix_fhir_bundle_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fhir_bundle_patient ON public.fhir_bundle USING btree (patient_id);


--
-- Name: ix_ocr_block_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ocr_block_fts ON public.ocr_block USING gin (to_tsvector('simple'::regconfig, text));


--
-- Name: ix_ocr_block_page; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ocr_block_page ON public.ocr_block USING btree (page_id, reading_order);


--
-- Name: ix_patient_identity_alias_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_identity_alias_patient ON public.patient_identity_alias USING btree (patient_id);


--
-- Name: ix_patient_identity_mrn; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_identity_mrn ON public.patient_identity USING btree (legacy_mrn);


--
-- Name: ix_patient_registry_mobile_digits; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_patient_registry_mobile_digits ON public.patient_registry USING btree (regexp_replace(mobile, '\D'::text, ''::text, 'g'::text));


--
-- Name: ix_pipeline_run_document; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pipeline_run_document ON public.pipeline_run USING btree (document_id, stage);


--
-- Name: ix_review_task_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_review_task_open ON public.review_task USING btree (status, created_at);


--
-- Name: ix_rv_action_item; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rv_action_item ON public.rv_action USING btree (rv_item_id, at);


--
-- Name: ix_rv_item_assignee; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rv_item_assignee ON public.rv_item USING btree (assigned_to) WHERE (assigned_to IS NOT NULL);


--
-- Name: ix_rv_item_element_item; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rv_item_element_item ON public.rv_item_element USING btree (rv_item_id);


--
-- Name: ix_rv_item_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rv_item_status ON public.rv_item USING btree (status, created_at);


--
-- Name: ix_source_document_patient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_source_document_patient ON public.source_document USING btree (patient_id);


--
-- Name: ux_patient_identity_abha; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_patient_identity_abha ON public.patient_identity USING btree (abha_number) WHERE (abha_number IS NOT NULL);


--
-- Name: ux_patient_registry_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_patient_registry_key ON public.patient_registry USING btree (mobile, lower(name), dob);


--
-- Name: abdm_care_context abdm_care_context_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_care_context
    ADD CONSTRAINT abdm_care_context_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: abdm_consent abdm_consent_care_context_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_consent
    ADD CONSTRAINT abdm_consent_care_context_id_fkey FOREIGN KEY (care_context_id) REFERENCES public.abdm_care_context(id);


--
-- Name: abdm_consent abdm_consent_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_consent
    ADD CONSTRAINT abdm_consent_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: abdm_transfer abdm_transfer_bundle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_transfer
    ADD CONSTRAINT abdm_transfer_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES public.fhir_bundle(id);


--
-- Name: abdm_transfer abdm_transfer_consent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abdm_transfer
    ADD CONSTRAINT abdm_transfer_consent_id_fkey FOREIGN KEY (consent_id) REFERENCES public.abdm_consent(id);


--
-- Name: ai_clinical_verification ai_clinical_verification_ai_suggestion_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_clinical_verification
    ADD CONSTRAINT ai_clinical_verification_ai_suggestion_id_fkey FOREIGN KEY (ai_suggestion_id) REFERENCES public.ai_suggestion(id) ON DELETE CASCADE;


--
-- Name: ai_event ai_event_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_event
    ADD CONSTRAINT ai_event_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id) ON DELETE CASCADE;


--
-- Name: ai_event ai_event_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_event
    ADD CONSTRAINT ai_event_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: ai_suggestion ai_suggestion_ai_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_suggestion
    ADD CONSTRAINT ai_suggestion_ai_event_id_fkey FOREIGN KEY (ai_event_id) REFERENCES public.ai_event(id) ON DELETE CASCADE;


--
-- Name: ai_suggestion ai_suggestion_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_suggestion
    ADD CONSTRAINT ai_suggestion_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES public.clinical_fact(id) ON DELETE SET NULL;


--
-- Name: clinical_fact clinical_fact_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_fact
    ADD CONSTRAINT clinical_fact_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: clinical_fact clinical_fact_extraction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_fact
    ADD CONSTRAINT clinical_fact_extraction_id_fkey FOREIGN KEY (extraction_id) REFERENCES public.extraction(id) ON DELETE SET NULL;


--
-- Name: clinical_fact clinical_fact_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_fact
    ADD CONSTRAINT clinical_fact_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: clinical_fact clinical_fact_supersedes_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clinical_fact
    ADD CONSTRAINT clinical_fact_supersedes_fkey FOREIGN KEY (supersedes) REFERENCES public.clinical_fact(id);


--
-- Name: cn_allergy cn_allergy_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_allergy
    ADD CONSTRAINT cn_allergy_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: cn_allergy cn_allergy_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_allergy
    ADD CONSTRAINT cn_allergy_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: cn_allergy cn_allergy_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_allergy
    ADD CONSTRAINT cn_allergy_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id);


--
-- Name: cn_allergy cn_allergy_source_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_allergy
    ADD CONSTRAINT cn_allergy_source_fact_id_fkey FOREIGN KEY (source_fact_id) REFERENCES public.clinical_fact(id) ON DELETE SET NULL;


--
-- Name: cn_clinical_note cn_clinical_note_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_clinical_note
    ADD CONSTRAINT cn_clinical_note_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: cn_clinical_note cn_clinical_note_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_clinical_note
    ADD CONSTRAINT cn_clinical_note_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: cn_condition cn_condition_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_condition
    ADD CONSTRAINT cn_condition_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: cn_condition cn_condition_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_condition
    ADD CONSTRAINT cn_condition_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: cn_condition cn_condition_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_condition
    ADD CONSTRAINT cn_condition_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id);


--
-- Name: cn_condition cn_condition_source_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_condition
    ADD CONSTRAINT cn_condition_source_fact_id_fkey FOREIGN KEY (source_fact_id) REFERENCES public.clinical_fact(id) ON DELETE SET NULL;


--
-- Name: cn_fhir_bundle cn_fhir_bundle_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_fhir_bundle
    ADD CONSTRAINT cn_fhir_bundle_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: cn_fhir_bundle cn_fhir_bundle_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_fhir_bundle
    ADD CONSTRAINT cn_fhir_bundle_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id) ON DELETE SET NULL;


--
-- Name: cn_lab_result cn_lab_result_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_lab_result
    ADD CONSTRAINT cn_lab_result_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: cn_lab_result cn_lab_result_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_lab_result
    ADD CONSTRAINT cn_lab_result_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: cn_lab_result cn_lab_result_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_lab_result
    ADD CONSTRAINT cn_lab_result_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id);


--
-- Name: cn_lab_result cn_lab_result_source_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_lab_result
    ADD CONSTRAINT cn_lab_result_source_fact_id_fkey FOREIGN KEY (source_fact_id) REFERENCES public.clinical_fact(id) ON DELETE SET NULL;


--
-- Name: cn_medication cn_medication_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_medication
    ADD CONSTRAINT cn_medication_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: cn_medication cn_medication_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_medication
    ADD CONSTRAINT cn_medication_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: cn_medication cn_medication_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_medication
    ADD CONSTRAINT cn_medication_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id);


--
-- Name: cn_medication cn_medication_source_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_medication
    ADD CONSTRAINT cn_medication_source_fact_id_fkey FOREIGN KEY (source_fact_id) REFERENCES public.clinical_fact(id) ON DELETE SET NULL;


--
-- Name: cn_vital_sign cn_vital_sign_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_vital_sign
    ADD CONSTRAINT cn_vital_sign_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: cn_vital_sign cn_vital_sign_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_vital_sign
    ADD CONSTRAINT cn_vital_sign_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: cn_vital_sign cn_vital_sign_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_vital_sign
    ADD CONSTRAINT cn_vital_sign_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id);


--
-- Name: cn_vital_sign cn_vital_sign_source_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cn_vital_sign
    ADD CONSTRAINT cn_vital_sign_source_fact_id_fkey FOREIGN KEY (source_fact_id) REFERENCES public.clinical_fact(id) ON DELETE SET NULL;


--
-- Name: doc_classification doc_classification_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.doc_classification
    ADD CONSTRAINT doc_classification_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: doc_classification doc_classification_pipeline_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.doc_classification
    ADD CONSTRAINT doc_classification_pipeline_run_id_fkey FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id);


--
-- Name: document_page document_page_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_page
    ADD CONSTRAINT document_page_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: encounter encounter_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.encounter
    ADD CONSTRAINT encounter_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: encounter encounter_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.encounter
    ADD CONSTRAINT encounter_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: extraction extraction_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction
    ADD CONSTRAINT extraction_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: extraction extraction_pipeline_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction
    ADD CONSTRAINT extraction_pipeline_run_id_fkey FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id);


--
-- Name: fact_conflict fact_conflict_fact_a_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_conflict
    ADD CONSTRAINT fact_conflict_fact_a_fkey FOREIGN KEY (fact_a) REFERENCES public.clinical_fact(id) ON DELETE CASCADE;


--
-- Name: fact_conflict fact_conflict_fact_b_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_conflict
    ADD CONSTRAINT fact_conflict_fact_b_fkey FOREIGN KEY (fact_b) REFERENCES public.clinical_fact(id) ON DELETE CASCADE;


--
-- Name: fact_conflict fact_conflict_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_conflict
    ADD CONSTRAINT fact_conflict_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: fact_provenance fact_provenance_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_provenance
    ADD CONSTRAINT fact_provenance_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES public.clinical_fact(id) ON DELETE CASCADE;


--
-- Name: fact_provenance fact_provenance_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_provenance
    ADD CONSTRAINT fact_provenance_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.document_page(id) ON DELETE SET NULL;


--
-- Name: fact_provenance fact_provenance_source_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fact_provenance
    ADD CONSTRAINT fact_provenance_source_doc_id_fkey FOREIGN KEY (source_doc_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: fhir_bundle fhir_bundle_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_bundle
    ADD CONSTRAINT fhir_bundle_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: fhir_bundle fhir_bundle_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_bundle
    ADD CONSTRAINT fhir_bundle_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: fhir_bundle fhir_bundle_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_bundle
    ADD CONSTRAINT fhir_bundle_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: fhir_resource fhir_resource_encounter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_resource
    ADD CONSTRAINT fhir_resource_encounter_id_fkey FOREIGN KEY (encounter_id) REFERENCES public.encounter(id) ON DELETE SET NULL;


--
-- Name: fhir_resource fhir_resource_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_resource
    ADD CONSTRAINT fhir_resource_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: fhir_resource fhir_resource_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fhir_resource
    ADD CONSTRAINT fhir_resource_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: fin_billing_item fin_billing_item_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fin_billing_item
    ADD CONSTRAINT fin_billing_item_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: fin_billing_item fin_billing_item_visit_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fin_billing_item
    ADD CONSTRAINT fin_billing_item_visit_id_fkey FOREIGN KEY (visit_id) REFERENCES public.ops_patient_visit(id) ON DELETE SET NULL;


--
-- Name: medication_detail medication_detail_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.medication_detail
    ADD CONSTRAINT medication_detail_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES public.clinical_fact(id) ON DELETE CASCADE;


--
-- Name: ocr_block ocr_block_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ocr_block
    ADD CONSTRAINT ocr_block_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: ocr_block ocr_block_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ocr_block
    ADD CONSTRAINT ocr_block_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.document_page(id) ON DELETE CASCADE;


--
-- Name: ocr_block ocr_block_pipeline_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ocr_block
    ADD CONSTRAINT ocr_block_pipeline_run_id_fkey FOREIGN KEY (pipeline_run_id) REFERENCES public.pipeline_run(id);


--
-- Name: ops_patient_visit ops_patient_visit_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ops_patient_visit
    ADD CONSTRAINT ops_patient_visit_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: patient_identity_alias patient_identity_alias_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_identity_alias
    ADD CONSTRAINT patient_identity_alias_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id) ON DELETE CASCADE;


--
-- Name: patient_identity_alias patient_identity_alias_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.patient_identity_alias
    ADD CONSTRAINT patient_identity_alias_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: pipeline_run pipeline_run_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_run
    ADD CONSTRAINT pipeline_run_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: review_task review_task_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_task
    ADD CONSTRAINT review_task_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: review_task review_task_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.review_task
    ADD CONSTRAINT review_task_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: rv_action rv_action_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_action
    ADD CONSTRAINT rv_action_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id) ON DELETE CASCADE;


--
-- Name: rv_item_element rv_item_element_fact_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_item_element
    ADD CONSTRAINT rv_item_element_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES public.clinical_fact(id) ON DELETE CASCADE;


--
-- Name: rv_item_element rv_item_element_rv_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_item_element
    ADD CONSTRAINT rv_item_element_rv_item_id_fkey FOREIGN KEY (rv_item_id) REFERENCES public.rv_item(id) ON DELETE CASCADE;


--
-- Name: rv_item rv_item_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_item
    ADD CONSTRAINT rv_item_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- Name: rv_item rv_item_source_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rv_item
    ADD CONSTRAINT rv_item_source_document_id_fkey FOREIGN KEY (source_document_id) REFERENCES public.source_document(id) ON DELETE CASCADE;


--
-- Name: source_document source_document_patient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source_document
    ADD CONSTRAINT source_document_patient_id_fkey FOREIGN KEY (patient_id) REFERENCES public.patient_identity(id);


--
-- PostgreSQL database dump complete
--

\unrestrict ViWrrf2532ZUXp0kc205ky2pG21ywDox1oIgVLz8eAkyY0Ak40Jq1WNNDAA9gll

