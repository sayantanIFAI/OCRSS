# Fallback host (backlog E1-S3)

If the RunPod pod dies, the system must run somewhere else. Owner decision (2026-09-21): **native
processes, no Docker**, exactly as ARCHITECTURE §3.3 runs the pod. The fallback host therefore
uses the same scripts (`infra/runpod/*.sh`); nothing about them is RunPod-only any more.
`infra/compose/` (TECH_STACK "Alt-deploy") is not a deployment path for this project.

## What the host needs

| | Requirement | Why |
|---|---|---|
| OS | Ubuntu 24.04, root (or sudo) | same apt packages and paths as the pod (§3.3) |
| GPU | NVIDIA, ≥ 24 GB VRAM, driver for CUDA 12.8 (the pod: Blackwell 32 GB, App. B) | `hf` backend: Qwen2.5-VL-7B bf16 ~15 GB VRAM + headroom |
| Disk | ≥ 80 GB | model cache ~16 GB, MinIO data, dumps, venv |
| Network | outbound HTTPS (apt, PyPI, download.pytorch.org, github.com, go.dev, huggingface.co) | first bootstrap only |

Without a GPU the host can run `CDI_MLSERVE_BACKEND=stub` (for checks only — no real extraction).

**Test hosts below 24 GB** (e.g. a Colab T4, 15 GB; owner, 2026-09-26): the 7B's bf16 weights
(~16 GB) do not fit, so the gateway falls back to the 3B. To test the 7B there, set
`CDI_VLM_QUANTIZE=8bit` (bitsandbytes LLM.int8 weights, ~9–11 GB; `start_mlserve.sh` installs the
pinned wheel) and cap the image size, e.g. `CDI_VLM_MAX_PIXELS_OCR=1003520` (1 MP), so a page's
activations fit beside the weights. Results from such a host are a demo, never a measurement: the
8-bit 7B at 1 MP is not the bf16 7B at 2 MP the pod serves.

## Differences from the pod, handled by the scripts

| On the pod | On the fallback host |
|---|---|
| torch inherited from the RunPod image (App. B) | `bootstrap_pod.sh` installs the same pinned `torch 2.8.0` / `torchvision 0.23.0` (cu128) when `CDI_MLSERVE_BACKEND=hf` and torch is missing |
| `LIVE URL: https://<POD_ID>-8888.proxy.runpod.net` (TLS by RunPod) | `LIVE URL: http://<host>:8888` (`CDI_PUBLIC_HOST` overrides the address); staff keep using the stable ingress hostname (E1-S9), which this host joins by opening the same tunnel |
| `/workspace` is MooseFS (0777) | an ordinary disk: the secrets store copy in `/workspace/secrets` keeps its 0700/0600 modes |

## Fail over

1. **Data.** Copy from the pod (or its last off-pod copy — scheduled copies and a restore drill are
   E10-S11) to the same paths on the new host:
   `/workspace/backup/cdi.dump`, `/workspace/minio-data/`, `/workspace/secrets/`, and optionally
   `/workspace/hf-cache/` (saves the ~16 GB model download).
2. **Code.** Put the repository at `/workspace/cdi` and `cp .env.runpod .env` (same layout, so the
   same file applies).
3. **Start.** `sudo bash /workspace/cdi/infra/runpod/start_all.sh` — installs packages, rebuilds
   PostgreSQL from `cdi.dump`, starts Redis and MinIO with the store's credentials, the model
   gateway and the web app, and prints the address.
4. **Lock the port.** Staff sign in (backlog E11-S1), but the web app holds patient data and binds
   `0.0.0.0`: allow 8888 only from the clinic network / VPN (e.g. `ufw allow from <clinic-cidr> to
   any port 8888`, `ufw deny 8888`). The start script prints this warning on a fallback host.
5. **Verify.**
   - `curl -s localhost:8888/healthz` → `db`, `s3`, `mlserve` all `ok`
   - `python scripts/golden_path.py --mode full --baseline stub --fresh-db cdi_golden` with
     `CDI_MLSERVE_BACKEND=stub` (pipeline + stack), then the real-model run
     `bash infra/runpod/golden_nightly.sh` (§8.2 `real_vlm` baseline)
6. **Point staff at it.** Nothing changes for staff: copy `/workspace/secrets/tunnel/` along with the
   data (step 1). `start_all.sh` then opens the tunnel from this host, and the stable ingress
   hostname (E1-S9, `docs/ingress.md`) serves from here. It prints `COUNTER URL: https://…` when it
   does. The printed `LIVE URL` is for diagnostics only.

## Verification host (Colab): keeping it in sync, and a test URL

Owner rule (2026-09-25): the host runs exactly the verified local tree, never hand-edited code, and
is used for live and integration tests. Its website is reached at a standalone test URL.

1. Locally, after the CI gate is green: `python scripts/dev/colab_bundle.py` writes
   `Desktop/cdi-src.tar.gz` with a `MANIFEST.sha256` of every file (a CRLF text file is refused).
2. Upload it into `/content` (Colab's Files panel), then notebook **Step 10**, which runs
   `infra/colab/sync.sh`. It checks every file against the manifest (a damaged upload changes
   nothing), removes the files the previous bundle had and this one lacks, applies the tree,
   re-checks it, reinstalls packages if `requirements.lock` changed, runs `alembic upgrade head` and
   restarts the web instances and workers (and the model gateway if its code changed). It prints
   `SYNCED: bundle <id>` and keeps the id in `/content/cdi/.bundle_id`. `.env`, `.venv`, data and
   logs are never in a bundle and are never touched.
3. Notebook **Step 9** runs `infra/colab/start_test_tunnel.sh`: a Cloudflare quick tunnel (pinned,
   checksum-verified `cloudflared`, `versions.env`) to the balancer, printing
   `TEST URL: https://<random>.trycloudflare.com`. Anyone with the URL reaches the sign-in page, with
   no Colab window or Google sign-in. It adds the host to `CDI_PUBLIC_ORIGINS` and restarts the web
   instances. The URL is new on every start. `status` / `stop` are the other commands.

The test tunnel decrypts at Cloudflare, so it carries **synthetic or de-identified data only**, and
is never the production path: a production pod is reached only through the E1-S9 ingress
(`docs/ingress.md`, `infra/runpod/start_tunnel.sh`).
