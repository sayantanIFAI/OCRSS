# RunPod pod operations (ARCHITECTURE §3.3, §10)

Native processes on one GPU pod — **no Docker**.

| Process | Port | Started by |
|---|---|---|
| PostgreSQL 16 (PGDATA on the overlay, restored from `/workspace/backup/cdi.dump`) | 5432 | `bootstrap_pod.sh` |
| Redis 7 (AOF in `/workspace/redis`) | 6379 | `bootstrap_pod.sh` |
| MinIO (data in `/workspace/minio-data`, built from source at `MINIO_TAG`) | 9000 / 9001 | `bootstrap_pod.sh` |
| model gateway `cdi_adapter.mlserve` | 8077 | `start_mlserve.sh` |
| web app `cdi_adapter.webapp` | 8888 (RunPod-proxied) | `start_all.sh` |
| reverse tunnel to the ingress VM (`autossh`, outbound) | - | `start_tunnel.sh` (E1-S9) |

## First boot and every restart

```bash
cd /workspace/cdi && cp .env.runpod .env   # first boot only
bash /workspace/cdi/infra/runpod/start_all.sh
```

The script prints `LIVE URL: https://<POD_ID>-8888.proxy.runpod.net` (this pod only, for
diagnostics). Once the ingress is configured it also prints
`COUNTER URL: https://<stable hostname>`. That is the address staff use (E1-S9, `docs/ingress.md`).

## Snapshots

`snapshot.sh` runs from cron every 15 minutes (installed by `bootstrap_pod.sh`). Run it by hand
before a planned stop. It keeps the last 10 timestamped dumps; `cdi.dump` is always the newest one.

## MinIO build

MinIO publishes no binaries any more, so `bootstrap_pod.sh` builds the pinned tag from the
official repository with the official Go toolchain. The toolchain archive must match `GO_SHA256`
and the tag must resolve to `MINIO_COMMIT` (both in `versions.env`), or the boot stops.

## The vLLM backend (backlog E2-S1)

`hf` is the active model backend (ARCHITECTURE §3.4). To try vLLM, set `CDI_MLSERVE_BACKEND=vllm`
in `/workspace/cdi/.env` and run `start_all.sh`. To go back, set it to `hf` and restart the
gateway.

vLLM **0.11.0** is the last release on torch 2.8 (App. B), and its model code predates
transformers 5. So the gateway runs from its **own venv**:
- `.venv-vllm` is built by bootstrap step 3b from `requirements-vllm.lock` (hash-checked, with
  transformers 4.57.6).
- It is drift-checked into `logs/manifests-vllm/`.
- The pipeline venv keeps transformers 5.16.1.
- `start_mlserve.sh` picks the venv from the backend setting.

Engine settings:
- `CDI_VLLM_GPU_MEMORY_UTILIZATION` 0.88
- `CDI_VLLM_MAX_MODEL_LEN` 16384
- `CDI_VLLM_MAX_NUM_SEQS` 4

On 32 GB this leaves about 15 GB for weights plus the KV cache, enough for 3–4 page extractions
at once.

Every extraction is grammar-locked (E2-S3, `CDI_VLLM_GUIDED=true`). The gateway turns the schema
into an XGrammar grammar, and vLLM can only produce tokens that keep the JSON schema-valid. A
schema the grammar cannot enforce exactly is refused with HTTP 422. It is never run unconstrained.

The web app follows the backend (E2-S2). With `vllm` it sends up to `CDI_VLLM_MAX_NUM_SEQS`
extractions at once, across all jobs. `CDI_EXTRACT_CONCURRENCY` overrides that number; 0 means
auto. With `hf` it always sends one at a time.

**Batching short documents (E2-S8), off by default.** With `CDI_EXTRACT_BATCH_MAX=3`, up to 3
short documents of one type (one page, at most `CDI_EXTRACT_BATCH_MAX_BLOCKS` OCR blocks) go to
the gateway in one `POST /vlm/generate_batch`. With `hf` that is one padded `generate` for all
of them, so their output tokens are produced in the same decode steps. With `vllm` they reach the
engine together. Every document stays its own sequence (own prompt, image, schema, evidence
map), and a batch that runs out of GPU memory is split, not dropped. A/B: run the E1-S7 burst
twice, with `CDI_EXTRACT_BATCH_MAX=1` and `=3` (restart the web app between runs), and compare
docs/minute. Turn it on only if the gain is real and the E2-S7 accuracy check shows no drop.

**A/B check on the pod.** Run the same job on each backend and compare the wall time and the
extracted facts:

```bash
python scripts/golden_path.py --mode full --baseline real_vlm --fresh-db cdi_golden
python scripts/load_test.py --host http://127.0.0.1:8888 --patients 5 --user loadtest1
```

## Pinned versions and drift detection (backlog E1-S4)

Every version the bootstrap installs is fixed in two files:

| File | Pins |
|---|---|
| `versions.env` | Ubuntu archive snapshot + the exact apt versions, Python series, Go version + checksum, MinIO tag + commit, torch/torchvision build, transformers |
| `requirements.lock` | every Python package, exact version + wheel sha256; generated, never edited by hand |

apt installs only from `snapshot.ubuntu.com` at `UBUNTU_SNAPSHOT`. pip installs only the lock,
with `--require-hashes --no-deps`, so the resolver never runs on the pod. `versions.env` is sourced
after `.env`, so no environment variable can override a pin.

The last bootstrap step writes `/workspace/logs/manifests/manifest.<UTC>.json`: every dpkg
package, every Python distribution (venv and inherited), the Go toolchain, the MinIO binary's
sha256 and the cached model revisions. It then checks the pins:

* **A pin does not hold:** `DRIFT:` lines, a line in `manifests/drift.log`, exit 1. The web app
  does not start. `CDI_ALLOW_DRIFT=1 bash start_all.sh` boots anyway; use it only to get the
  counter running while the drift is fixed.
* **Something changed that no pin covers since the previous boot** (e.g. a base-image package):
  `WARNING: changed since the previous boot` lines, and the boot continues.

The first boot after this change may report `... is in the venv but not in requirements.lock`
for packages the old unpinned install left behind. Rebuild the venv once:
`rm -rf /workspace/cdi/.venv && bash start_all.sh`.

### Acceptance: two fresh boots, identical versions

1. Start a new pod with an **empty** workspace volume, copy in the repo, then run
   `cp .env.runpod .env && bash infra/runpod/start_all.sh` and wait for `LIVE URL`. Nothing
   else is needed.
2. Copy `/workspace/logs/manifests/last.json` off the pod as `boot1.json`.
3. Repeat steps 1 and 2 on a second new pod and save the result as `boot2.json`.
4. Run `python -m cdi_adapter.podmanifest compare boot1.json boot2.json`. It prints
   `identical versions` and exits 0. The timestamp and host id are not compared.

### Upgrading a pin

1. Edit `versions.env`. For apt, pick a new `UBUNTU_SNAPSHOT` and read each package's version at
   that snapshot. For Go, copy the new sha256 from `https://go.dev/dl/?mode=json`. For MinIO, run
   `git ls-remote https://github.com/minio/minio.git refs/tags/<tag>` to get the commit.
2. For Python changes (`pyproject.toml`, torch, transformers), run
   `python scripts/lock_pod_deps.py` (`make lock`). `--check` only reports whether the lock is
   stale.
3. Run `python scripts/ci.py`, boot once, check that there are no `DRIFT:` lines, and commit
   `versions.env` and `requirements.lock` together.

## Health and logs

```bash
curl -s localhost:8888/healthz          # {status, db, s3, mlserve}
curl -s localhost:8077/healthz          # {backend, model, loaded, device}
tail -f /workspace/logs/webapp.log      # pipeline events
PGPASSFILE=/run/cdi-secrets/pgpass psql postgresql://cdi@127.0.0.1:5432/cdi -f scripts/verify_s3.sql
```

RunPod's proxy returns HTTP 200 with a fake 502 page for unbound ports, so health checks here grep
a content marker instead of trusting the status code.
