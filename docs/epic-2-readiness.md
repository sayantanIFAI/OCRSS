# Epic 2 production readiness assessment

Scope: the supplied E2-S1, E2-S2, E2-S3, E2-S6 and E2-S8 stories, evaluated against the
supplied architecture and the active implementation. Starting revision:
`54d45c888d40ea2f44f149074c06985d63fe11d9` (Epic 1 hardening).

**Release decision: NOT READY.** The code defects listed below are repaired. Local checks do
not establish the required 32 GB GPU capacity, held-out clinical accuracy, AWQ model quality,
or batching throughput improvement. No real GPU, E2-S4 gold set, E2-S5 checkpoint or sizing
workbook was supplied. E2-S4/E2-S5 are dependencies, not additional stories in this image.

## Story acceptance

| Story | Implementation and validation coverage | Outstanding production evidence |
|---|---|---|
| E2-S1 — vLLM backend | Existing AsyncLLMEngine, continuous batching, HTTP contract and HF switch retained. Fixed failed-startup loop/engine cleanup, pinned processor revision, generation cancellation/deadline, gateway shutdown and false-success backend startup checks. CPU fakes test four requests in flight and HF serialization. | Run 3–4 simultaneous real extractions without OOM on the target 32 GB GPU, record model revision/VRAM/latency, and demonstrate HF rollback after restart. CPU concurrency tests cannot prove this. |
| E2-S2 — Phase-2 concurrency | HF constructor overrides can no longer bypass serialization; vLLM extraction slots are capped by max_num_seqs. Existing patient identity ordering and job scheduling remain tested. | Real job traces/logs showing parallel vLLM extraction and retained HF serialization on the deployment. Historical architecture reports only two of three documents overlapping and no wall-time gain; no new speedup is claimed. |
| E2-S3 — grammar locking | Existing strict v3 schemas and real XGrammar token masks retained. Unknown/unproven schema keywords, invalid schemas and placeholder collisions fail closed. Fixed confidence substitution when property names resemble schema keywords. Invalid/truncated constrained responses fail; HTTP clients bypass repair/retry and `_partial` for grammar-locked outputs. Required CI grammar job uses pinned public tokenizer and fails if dependencies are unavailable. | Run the E2-S4 gold set with the real model; record zero schema-validity-related `_partial` results **alongside completion/failure counts**, so failing all documents cannot look like success. Clinical-fidelity errors require separate evaluation. Truncation and transport failures can still fail a request. |
| E2-S6 — quantization and benchmark | Existing AWQ tooling and production-backend benchmark retained. Quantization refuses nonempty/overlapping output paths. Comparison rejects mismatched corpus, request IDs, schemas, engine settings, hardware and concurrency levels; invalid timing values and raw JSON fail. KV slots are explicitly estimates capped by engine concurrency. | Obtain E2-S5 checkpoint and E2-S4 train/held-out splits; quantize on training only; measure against bf16 7B on target GPU; run held-out clinical thresholds; archive outputs; update sizing rows 26/27 with measured evidence; register the accepted model before production traffic. No AWQ checkpoint was produced here. |
| E2-S8 — short-document batching | Existing single-page, same-type grouping sends a batched gateway request with independent image/prompt/schema/evidence per document. HF batches under its GPU lock; vLLM submits sequences concurrently. Incomplete batch results now fail every affected document instead of leaving documents running. | A/B load runs with batching enabled/disabled under the E1-S7 load shape; report documents/minute/GPU and latency, then prove no per-field regression on E2-S4. Fewer HTTP requests and unit tests alone do not prove a throughput gain. |

## Defects corrected

- A timed-out caller could leave vLLM work running without a generation deadline. Cancellation
  now reaches the engine generator; gateway timeout responses are HTTP 504.
- Failed initialization could leave an event loop behind; successful engines had no gateway
  shutdown hook. Failed processor setup now also releases the engine.
- Processor loading did not honor the model revision pin.
- An explicit JobRunner concurrency value could override HF's one-extraction limit.
- Missing batch outputs could leave documents in an unfinished state; gateway cardinality is
  also checked before returning success.
- Guided responses still passed through JSON repair/retry, which could silently modify model
  output. Per-response decoding metadata selects strict parsing and validation.
- Schema feature checking used a blacklist, missing constraints unsupported by the grammar.
  Literal data and schema-keyword property names could be mishandled during substitution.
- A live HF process could satisfy a vLLM startup health check after an environment flip.
- Benchmark comparison could mix different inputs/hardware/settings and overstate concurrent
  slots; malformed JSON could be salvaged and counted as schema-valid.
- Quantization could overwrite an existing output or overlap its source checkpoint/corpus.

## Validation

The main suite uses
Python 3.12.15 and the synthetic stub. Integration uses PostgreSQL 16, real MinIO, Redis and CPU
OCR. Token-mask validation separately uses torch 2.8.0+cpu, XGrammar 0.1.25 and transformers 4.57.6,
with Qwen tokenizer revision `66285546d2b821cf421d4f5eb2576359d3770cd3`.

Observed checks:

- Real XGrammar token-mask suite: **45 passed, zero skipped**.
- New failure-path regressions: **34 passed**; these are also included in the broad unit suite.
- Integration gate: **326 passed**, one unrelated optional de-identification module skipped at
  collection. Pipeline audit passed; every required pipeline stage completed successfully.
- Full stub golden: **21 facts, 21 provenance, 37 resources, 3 bundles, zero structural errors**.
  This passes the existing stub baseline and still fails the story's exact 15/15 requirement.
- Lint, formatting, mypy, secret scan, workflow actionlint, shell syntax and diff checks: passed.
- Colab sync fixture isolation: **8 passed**. An initial broad unit run was terminated because
  a real `pkill` in this test matched its parent shell. The fixture now intercepts process
  detection/stopping and asserts a synthetic gateway restart; it cannot stop host services.
- Reused the earlier real HL7 validation result: **7/7 bundles valid, zero errors**. FHIR
  source, golden fixtures, validator script and project dependency inputs retain matching hashes.
- Full unit suite: **1,149 passed, 31 optional skips**, zero failures (291.76 seconds). The
  skips cover optional de-identification dependencies, XGrammar, the FHIR validator and promtool. All XGrammar
  checks were exercised separately above. The final Colab fixture edit was checked separately
  after the broad run collected its tests; the other broad-suite results remain applicable.

## Release procedure

1. Finish the outstanding evidence in every row above. Keep the historical vLLM result until
   a reproducible target run replaces it. Generation timeout defaults to 240 seconds, excluding
   initialization/model download; tune it together with client/proxy timeouts on the deployment.
2. Preserve per-field clinical evidence in batch comparisons; do not replace gold evaluation
   with agreement against another model's potentially incorrect output.
3. Resolve Epic1's existing golden discrepancy: stub 21 facts / 21 provenance versus story 15/15.
   This review preserves the clinical records and existing baseline.
4. Run hosted CI and require unit-gate, grammar-gate and integration-gate on the destination
   branch; merge through a passing PR. No hosted run or merge is claimed by this report.
5. This sandbox permits remote pushes only to
   `coderabbit/epic-one-production-readiness/ac733714`, not `Dev_Main`. The previous push was
   rejected for missing GitHub App workflows permission. The local Dev_Main commit and patch
   can be published through an appropriately authorized workspace/account.
