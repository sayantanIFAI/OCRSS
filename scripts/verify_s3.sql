-- Verify stage S3 (OCR) output (ARCHITECTURE §10.4):
--   PGPASSFILE=/run/cdi-secrets/pgpass psql postgresql://cdi@127.0.0.1:5432/cdi -f scripts/verify_s3.sql

\echo '== OCR blocks per document (engine, count, mean confidence) =='
SELECT d.filename, d.status, b.engine, count(b.id) AS blocks,
       round(avg(b.ocr_conf)::numeric, 3) AS mean_conf
FROM source_document d
LEFT JOIN ocr_block b ON b.document_id = d.id
GROUP BY d.filename, d.status, b.engine
ORDER BY d.filename;

\echo '== blocks without a valid 4-number bbox (expect 0) =='
SELECT count(*) AS bad_bbox FROM ocr_block WHERE bbox IS NULL OR array_length(bbox, 1) <> 4;

\echo '== reading order gaps per page (expect 0 rows) =='
SELECT page_id, count(*) AS blocks, max(reading_order) + 1 AS expected
FROM ocr_block GROUP BY page_id HAVING count(*) <> max(reading_order) + 1;

\echo '== last OCR run per document =='
SELECT DISTINCT ON (document_id) document_id, status, metrics, finished_at
FROM pipeline_run WHERE stage = 'ocr' ORDER BY document_id, started_at DESC;
