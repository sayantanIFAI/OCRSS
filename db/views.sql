-- Read-model views: flattened lists clinicians read (ARCHITECTURE §4.1).
-- Only current facts; review_state is exposed so a reader can tell governed from held facts.

CREATE OR REPLACE VIEW v_problem_list AS
SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
       coalesce(f.code_display, f.local_text) AS problem,
       f.clinical_status, f.verification, f.onset, f.review_state, f.encounter_id,
       e.period_start AS encounter_date
FROM clinical_fact f
LEFT JOIN encounter e ON e.id = f.encounter_id
WHERE f.fact_type = 'condition' AND f.is_current;

CREATE OR REPLACE VIEW v_medication_list AS
SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
       coalesce(f.code_display, m.drug_text, f.local_text) AS medication,
       m.strength_num, m.strength_unit, m.dose_num, m.dose_unit_ucum, m.route,
       m.frequency_code, m.frequency_per_day, m.duration_days, m.prn, m.instructions,
       f.clinical_status, f.review_state, f.encounter_id, e.period_start AS encounter_date
FROM clinical_fact f
LEFT JOIN medication_detail m ON m.fact_id = f.id
LEFT JOIN encounter e ON e.id = f.encounter_id
WHERE f.fact_type = 'medication' AND f.is_current;

CREATE OR REPLACE VIEW v_allergy_list AS
SELECT f.patient_id, f.id AS fact_id, f.code_system, f.code,
       coalesce(f.code_display, f.local_text) AS substance,
       f.value_text AS reaction, f.clinical_status, f.review_state, f.encounter_id
FROM clinical_fact f
WHERE f.fact_type = 'allergy' AND f.is_current;

CREATE OR REPLACE VIEW v_results_grid AS
SELECT f.patient_id, f.id AS fact_id, f.fact_type, f.code_system, f.code,
       coalesce(f.code_display, f.local_text) AS test,
       f.value_num, f.value_unit_ucum, f.value_text,
       f.ref_range_low, f.ref_range_high, f.ref_range_text, f.abnormal_flag,
       coalesce(f.effective_time, e.period_start::timestamptz) AS effective_time,
       f.review_state, f.encounter_id
FROM clinical_fact f
LEFT JOIN encounter e ON e.id = f.encounter_id
WHERE f.fact_type IN ('lab_result', 'vital_sign') AND f.is_current;

CREATE OR REPLACE VIEW v_encounter_timeline AS
SELECT e.patient_id, e.id AS encounter_id, e.period_start, e.date_precision, e.class,
       e.doc_type, e.source_document_id, d.filename,
       count(f.id) AS fact_count,
       count(f.id) FILTER (WHERE f.review_state IN
             ('auto_accepted', 'clinician_confirmed', 'corrected')) AS governed_count,
       count(f.id) FILTER (WHERE f.review_state IN ('pending', 'in_review')) AS held_count
FROM encounter e
LEFT JOIN source_document d ON d.id = e.source_document_id
LEFT JOIN clinical_fact f ON f.encounter_id = e.id AND f.is_current
GROUP BY e.patient_id, e.id, e.period_start, e.date_precision, e.class, e.doc_type,
         e.source_document_id, d.filename;
