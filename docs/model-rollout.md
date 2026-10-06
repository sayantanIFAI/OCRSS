# Model registry, canary and rollback (backlog E10-S5)

A new model reaches documents in three steps: it is **registered**, it takes a **small share** of
live documents next to the incumbent, and it becomes the primary only when the **live comparison**
allows it. **One command** undoes any of it. Everything is `python -m cdi_adapter.modelops`
(code: `src/cdi_adapter/modelops/`, tables: migration `0006_model_registry.py`).

## The pieces

| Piece | What it is |
|---|---|
| `model_version` | One row per registered version: model id, **pinned revision**, backend, quantization, and its E2-S7 offline eval report. A version name is never reused for another model. |
| `model_rollout` | Append-only log. Each row is a full routing state: primary version + gateway URL, and optionally a canary version + URL + share (%). The latest row is live. UPDATE and DELETE are refused by a trigger, so the history a rollback reads cannot be rewritten. Every change also writes `audit_log`. |
| Gateways | One `mlserve` process per version, on its own 127.0.0.1 port. Its `/healthz` reports `version`, `model`, `backend`. A version is only put in the rollout when its gateway reports exactly that version, model and backend (a 7B that fell back to the 3B after an OOM does not pass). |
| Router (web app) | Per document: `sha256(document id) mod 100 < canary %` goes to the canary, the rest to the primary. The same document always lands on the same side. The state is re-read every `CDI_MODEL_ROUTE_TTL_S` (5 s), so no restart is needed. If the database is unreachable, the last known state is kept. |
| Canary fallback | A canary call that fails (unreachable or 5xx) is sent to the primary. The document is not lost, and `cdi_model_canary_fallbacks_total` counts it. A 4xx (a refused grammar) is an answer and is not retried elsewhere. |
| Provenance | `extraction.model_version` / `model_name` = the version and model that answered; `pipeline_run.model_version` = the side the document was routed to. |

## Commands

```bash
M="python -m cdi_adapter.modelops"          # --actor NAME, or $CDI_OPERATOR, or the OS user
$M register qwen7b-2026.09 --model-id Qwen/Qwen2.5-VL-7B-Instruct --revision <commit sha> \
   --backend hf [--eval-report e2s7.json]
$M serve qwen7b-2026.09 --url http://127.0.0.1:8078     # start its gateway (lazy model load)
$M deploy qwen7b-2026.09 --url http://127.0.0.1:8077    # the first primary only
$M canary cdi-dslm-v1 --url http://127.0.0.1:8078 --percent 10 --reason "E2-S5 fine-tune"
$M set-percent 25                                      # 0 pauses the canary; at most 50
$M compare                                             # canary vs primary, live
$M promote                                             # only if compare allows it
$M rollback --reason "..."                              # THE undo, see below
$M status                                              # state, gateway health, history
```

The pod's own gateway (`start_mlserve.sh`) joins the registry by setting `CDI_MODEL_VERSION` and
`CDI_VLM_MODEL_REVISION` in `.env`. It then reports its version, and `deploy` can name it.

## Rollback: one command

`python -m cdi_adapter.modelops rollback --reason "..."`

- **A canary is deployed:** it is dropped. The primary takes every new document within
  `CDI_MODEL_ROUTE_TTL_S`, and nothing needs to start.
- **No canary (a promotion is being undone):** it returns to the previous primary in the log. If
  that gateway is no longer running, it is started first (`serve`), and the switch happens only
  once it answers as that version. If the port is taken by something else, nothing changes and the
  command says why.

Run `status` first: `rollback` always acts. With no canary it **does** change the primary; it
never just reports (seen on the Colab check, where it correctly returned to the drill's model).

Documents already extracted by the rolled-back version keep their facts. The gate and the reviewers
treat them like any other, and `extraction.model_version` shows which ones they are. To re-run
them on the restored model, re-extract those documents (they can be found by that column).

## The live comparison (`compare`) and the promotion gate

The E2-S7 metrics (field-F1, numeric exactness, hallucination rate, FHIR validity) need gold
labels. They are measured offline on the E2-S4 gold set and registered with each version
(`--eval-report`). `compare` shows both reports side by side. The gate blocks a canary that is
more than 1 point worse on any of them.

Live, over the same window since the canary started, each side is measured on its own documents:

| Live measure | Stands in for | Gate: canary may be at most |
|---|---|---|
| `_partial` rate (schema-invalid answers) | schema validity | +5 points over the primary |
| failed extractions | availability | +2 points |
| facts rejected by reviewers / facts reviewed | hallucination | +5 points (once both sides have 10 reviewed facts) |
| facts corrected by reviewers / facts reviewed | field errors | +10 points (likewise) |
| auto-accept share, mean confidence, extraction time p50 / p95 | shown, not gated | |

At least **20 canary documents** are needed before the gate can pass. `promote --force --reason`
overrides a failed gate. The reason and `"forced": true` go into the rollout log and the audit
log. The gate's limits are `LIMITS` in `modelops/compare.py`.

**Grafana:** "Canary: share of documents (%) and calls sent back to the primary" and "Canary vs
primary: _partial and failed extractions" (`cdi_model_canary_percent`,
`cdi_model_extractions_total{role,outcome}`, `cdi_model_canary_fallbacks_total`).

## GPU memory

Each version is its own process on the GPU. On one 24 GB L4, a 7B (~17 GB) and a second 7B do not
fit together. A canary there needs a quantized model (E2-S6), a 3B, or a second GPU (E9-S4 /
E13-S6). On the 32 GB design target, a 7B next to a 4-bit 7B is expected to fit (about 17 + 6 GB
of weights); check it with the E1-S7 load test first.

## Tests and drill

- `tests/test_modelops_unit.py`: routing and its share, the cache and a database outage, the
  canary fallback (5xx vs 4xx), the gateway's version, the gate, the CLI.
- `tests/test_modelops_integration.py` (PostgreSQL, real gateways over HTTP, stub models):
  - the full drill: deploy, canary, compare, rollback, canary, promote, rollback, promote, a
    gateway down, then a rollback that starts it again;
  - a broken canary loses no document;
  - a batch never mixes sides;
  - the gate blocks a bad canary;
  - nothing changes unless the gateway serves that exact version;
  - the log cannot be rewritten;
  - `serve` starts a real gateway process.
- `scripts/model_rollout_drill.py`: the same drill with real models on the GPU (Colab / the pod).
  It uses the same 3B weights as two versions, because two different 7B models do not fit one L4.
