# Quantizing and benchmarking the fine-tuned DSLM (backlog E2-S6)

ARCHITECTURE §14 #1d: the fine-tuned `cdi-dslm` is served as **AWQ/INT4**. This runbook turns the
E2-S5 checkpoint into an INT4 checkpoint and measures it against the **bf16 Qwen2.5-VL-7B
baseline** on the production serving path. It reports extract-seconds per document, docs/minute,
VRAM, and concurrent slots at 32 GB.

**Status:** the tooling is built and tested. The measured run needs two things this build does not
have yet:

1. **The E2-S5 checkpoint**, which needs the E2-S4 gold set, which needs a clinical annotator.
2. **The target GPU** (RTX PRO 4500 Blackwell 32 GB) for a row tagged `MEASURED`.

## The three steps

| Step | Module | Venv | What it does |
|---|---|---|---|
| 1. Corpus | `cdi_adapter.dslm.corpus` | `.venv` (pipeline) | Builds the real S4 request for each document: normalized page-1 image, `[bN]` OCR prompt, inlined schema, per-type `max_tokens`. Uses the pipeline's own code, with no database. |
| 2. Quantize | `cdi_adapter.dslm.quantize` | `.venv-quant` | AWQ W4A16 with llm-compressor, calibrated on the corpus. Writes a compressed-tensors checkpoint + `cdi_quantization.json`. |
| 3. Benchmark | `cdi_adapter.dslm.bench run` / `compare` | `.venv-vllm` / `.venv` | Replays the corpus through the production `VLLMBackend`, one process per variant, then compares and archives. |

Each step has its own venv because the three tools need different versions of `transformers`:

| Venv | Lock | transformers | Why |
|---|---|---|---|
| `.venv` | `requirements.lock` | 5.16.1 | the pipeline (App. B) |
| `.venv-vllm` | `requirements-vllm.lock` | 4.57.6 | vLLM 0.11.0, the last release on torch 2.8 (E2-S1) |
| `.venv-quant` | `requirements-quant.lock` | 4.55.2 | llm-compressor 0.7.1.4 |

All three use torch 2.8.0+cu128 and are hash-locked (`scripts/lock_pod_deps.py --target main|vllm|quant`).

## Why these choices

- **llm-compressor 0.7.1.4.** It writes checkpoints in **compressed-tensors 0.11.0**, the exact
  version vLLM 0.11.0 reads (a test checks both locks agree), and it runs on torch 2.8. Newer
  releases need torch ≥ 2.9 or transformers 5. AutoAWQ is no longer maintained.
- **AWQ `W4A16_ASYM`.** Weights are 4-bit with a zero point in groups of 128; activations stay
  bf16. vLLM detects the format from `config.json`, so serving it needs no new setting:
  `CDI_VLM_MODEL_ID=<quantized dir>`.
- **Only the language decoder is quantized.** The vision tower and `lm_head` stay bf16.
  llm-compressor 0.7.1 has no AWQ mapping for Qwen2.5-VL. Its generic fallback's
  `up_proj → down_proj` pattern would also match the vision tower's MLP. So the mappings in
  `quantize.MAPPINGS` are anchored to the decoder's `layers.N`, and the vision blocks are
  `blocks.N`. A test checks both sides.
- **Calibration uses the real extraction requests.** llm-compressor's example uses photo
  captions. We calibrate on the S4 page images (at the serving `max_pixels`) with the `[bN]`
  prompt, so AWQ's scales fit the traffic the model will actually serve. For E2-S5 the corpus is
  the gold set's **training split**, never the held-out split.
- **The benchmark uses the production path.** It runs `VLLMBackend` with the gateway's engine
  settings, chat prompt, image handling and XGrammar lock. The 7B→3B fallback is turned off, so
  a variant that does not fit fails instead of quietly measuring another model.

## Data rule

The corpus is copied to GPU hosts and recorded in the checkpoint's calibration digest, so only
**synthetic** or **de-identified** documents may go in (ARCHITECTURE §12, E11-S4).
`--data-class` must be one of the two, and the reader refuses anything else. Colab is outside
India: synthetic only.

## Run it (GPU host, after `bootstrap_pod.sh`)

One command builds `.venv-quant` and `.venv-vllm` from their locks if missing, then runs all
three steps:

```bash
CHECKPOINT=/workspace/models/cdi-dslm DATA_CLASS=deidentified bash infra/runpod/dslm_quantize_bench.sh /workspace/gold/train/*
```

| Variable | Default | Meaning |
|---|---|---|
| `CHECKPOINT` | (required) | the bf16 checkpoint to quantize (E2-S5's merged output) |
| `BASELINE` | `Qwen/Qwen2.5-VL-7B-Instruct` | the bf16 reference |
| `DATA_CLASS` | `synthetic` | `synthetic` or `deidentified` |
| `LABELS` | — | `file=doc_type` pairs for documents the heuristic classifier cannot place |
| `QUANT_OUT` | `/workspace/models/<name>-awq` | where the INT4 checkpoint goes |
| `ARCHIVE` | `docs/bench/e2-s6` | where the dated `.json` + `.md` go |

It benchmarks `baseline-bf16`, `dslm-bf16` (the checkpoint before quantization) and `dslm-awq`.
Each runs in its own process, so the GPU is fully freed between them. Logs and raw results stay
in `/workspace/dslm/run-<UTC stamp>/`.

The steps by hand:

```bash
.venv/bin/python -m cdi_adapter.dslm.corpus build --out run/corpus --data-class synthetic data/samples/*
.venv-quant/bin/python -m cdi_adapter.dslm.quantize --checkpoint <ckpt> --corpus run/corpus --out <ckpt>-awq
.venv-vllm/bin/python -m cdi_adapter.dslm.bench run --name baseline-bf16 --model Qwen/Qwen2.5-VL-7B-Instruct --corpus run/corpus --out run/baseline-bf16.json
.venv-vllm/bin/python -m cdi_adapter.dslm.bench run --name dslm-awq --model <ckpt>-awq --corpus run/corpus --out run/dslm-awq.json
.venv/bin/python -m cdi_adapter.dslm.bench compare run/baseline-bf16.json run/dslm-awq.json --archive docs/bench/e2-s6
```

## What the numbers mean

| Column | How it is measured |
|---|---|
| s/doc (median, p95) | Each request alone, after one warm-up request (CUDA graphs, grammar compile) |
| docs/min, at k | Best throughput over concurrency levels 1, 2, 4 (continuous batching, E2-S1) |
| VRAM GiB | Device memory in use once the engine is up: weights + the KV cache vLLM reserves at `gpu_memory_utilization` 0.88 |
| KV tokens | vLLM's own KV sizing: `num_gpu_blocks × block_size` |
| Slots | KV tokens ÷ the p95 request footprint (prompt incl. image tokens + generated tokens), capped at `CDI_VLLM_MAX_NUM_SEQS` (4). **Slots (worst)** uses the full `max_tokens` budget. These are capacity estimates; only the recorded concurrency load runs demonstrate successful measured throughput. |
| Schema-valid | Raw outputs that pass the v3 schema (should be all of them under the XGrammar lock) |
| Agreement | Share of the bf16 baseline's output fields reproduced exactly (`confidence` ignored). **A regression signal only.** Accuracy is E2-S7's harness on the E2-S4 held-out set (DoD-EVAL). |
| Tag | `MEASURED` only on the 32 GB target GPU; otherwise `MEASURED on <gpu> (not the 32 GB target)` |

## Sizing & Capacity

The block asks for the results in the Excel's *Sizing & Capacity* sheet (rows 26/27 in the supplied E2-S6 acceptance criteria). That workbook is not in this repository. Copy the `dslm-awq` row from the archived `.md`
into it, using that row's `Tag`. Keep KV slot estimates labeled as estimates.

Comparison refuses different corpus digests, request IDs/schemas, engine settings, GPU hardware,
concurrency levels, duplicate variants and non-finite/nonpositive measurements. Raw responses
must be complete JSON; surrounding prose is invalid. The CLI exits nonzero if any schema-valid
count is incomplete. Calibration output must be empty and disjoint from its source checkpoint
and corpus. Benchmark success does not satisfy held-out clinical evaluation or model registration.

## Checks before the numbers count

- `cdi_quantization.json` names the E2-S5 checkpoint as `source_model`, and the calibration
  `data_class` is `deidentified` or `synthetic`.
- The run is on the target GPU (`Tag` = `MEASURED`).
- Schema-valid is n/n for every variant.
- **Blackwell risk:** vLLM 0.11.0's int4 kernels (Marlin) must support the RTX PRO 4500's
  compute capability. If `dslm-awq` fails to start there, record that result rather than
  switching quantization method silently. The fix is a vLLM upgrade, which is an App. B pin change.
