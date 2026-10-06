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

## Differences from the pod, handled by the scripts

| On the pod | On the fallback host |
|---|---|
| torch inherited from the RunPod image (App. B) | `bootstrap_pod.sh` installs the same pinned `torch 2.8.0` / `torchvision 0.23.0` (cu128) when `CDI_MLSERVE_BACKEND=hf` and torch is missing |
| `LIVE URL: https://<POD_ID>-8888.proxy.runpod.net` (TLS by RunPod) | `LIVE URL: http://<host>:8888` (`CDI_PUBLIC_HOST` overrides the address); TLS + a stable hostname is E1-S9 |
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
4. **Lock the port.** The web UI has no login yet (ARCHITECTURE §12; backlog E11-S1) and binds
   `0.0.0.0`: allow 8888 only from the clinic network / VPN (e.g. `ufw allow from <clinic-cidr> to
   any port 8888`, `ufw deny 8888`). The start script prints this warning on a fallback host.
5. **Verify.**
   - `curl -s localhost:8888/healthz` → `db`, `s3`, `mlserve` all `ok`
   - `python scripts/golden_path.py --mode full --baseline stub --fresh-db cdi_golden` with
     `CDI_MLSERVE_BACKEND=stub` (pipeline + stack), then the real-model run
     `bash infra/runpod/golden_nightly.sh` (§8.2 `real_vlm` baseline)
6. **Point staff at it.** Until E1-S9 provides a stable hostname, the counter uses the printed
   address.
