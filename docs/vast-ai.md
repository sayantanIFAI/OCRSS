# vast.ai test host — rent, deploy, back up, destroy

A vast.ai GPU instance is a **verification host** (like Colab, [fallback-host.md](fallback-host.md)):
it is outside India, so **test / de-identified data only**. It bills by the hour until destroyed
(a *stopped* instance still bills its disk, ~$0.05/hr, and may wait in "Scheduling…" for its GPU
when started again).

## 1. Rent (the owner clicks Rent: it spends money)

* Template: **"CareFlow CDI - Ubuntu 24.04 CUDA 12.8 py312"** (My Templates; image
  `vastai/base-image:cuda-12.8.1-cudnn-devel-ubuntu24.04-py312`, SSH + Jupyter, 100 GB disk).
* GPU: **>= 24 GB VRAM** so the full bf16 Qwen2.5-VL-7B runs (RTX 5090 32 GB used so far, ~$0.51/hr).
  Below 24 GB set `CDI_VLM_QUANTIZE=8bit` in `.env` (docs/quantization.md).
* Add the dev box's public key `~/.ssh/rx_gpu.pub` to the instance (instance card -> SSH keys; the
  team account has no account-wide keys). The card's "SSH connection" shows `-p <port> root@<ip>`.

## 2. Deploy (one command, from the repo root, after CI is green)

```bash
bash scripts/dev/vast_deploy.sh <ip> <port>                                  # empty system
bash scripts/dev/vast_deploy.sh <ip> <port> --restore data/vast-backup/<file>.tar.gz   # with a snapshot
```

It builds the verified bundle, copies it with the medicine catalogue
(`data/catalogue/tata1mg/`, git-ignored) and the snapshot, and runs `infra/vastai/setup_vast.sh` on
the host: bootstrap (PostgreSQL 16, Redis, MinIO, venv, migrations; the known FHIR digest drift is
tolerated), the snapshot restore (database before the bootstrap starts it, then the scans), the
model gateway with a warm-up (the first run downloads the 7B, ~16 GB), the app tier, the catalogue
import and the public test link. It ends with `SETUP OK`. Running it again updates the host in place.

Without `--restore`, create the first admin on the host (the owner types the password; it is never
passed on a command line):

```bash
ssh -i ~/.ssh/rx_gpu -p <port> root@<ip>
cd /workspace/cdi && set -a && . ./.env && set +a && .venv/bin/python -m cdi_adapter.webapp.users add admin --name "EMR Admin" --role admin
```

A restored snapshot brings its users back (the admin signs in with the old password).

## 3. Use

* Public link: `https://<random>.trycloudflare.com`, printed by the setup or in
  `/workspace/logs/tunnel_retry.log`. vast.ai IPs are shared, so Cloudflare often answers 429;
  `infra/vastai/tunnel_retry.sh` retries every 2 minutes. The link changes on every restart.
* Meanwhile, from the dev box: `ssh -i ~/.ssh/rx_gpu -p <port> -N -L 18888:127.0.0.1:8888 root@<ip>`
  and open http://localhost:18888 (Reviewer `/reviewer`, Pipeline Monitor `/monitor`).
* After a stop/start of the same instance: run `bash scripts/dev/vast_deploy.sh <ip> <port>` again
  (the port may change) — it restarts everything.

## 4. Back up, then destroy

```bash
bash scripts/dev/vast_backup.sh <ip> <port>     # snapshot -> data/vast-backup/<time>.tar.gz
```

Check the two sha256 lines match, then destroy the instance in the vast.ai console (Destroy is
irreversible: everything on it is deleted). Restore later with `vast_deploy.sh ... --restore`.

Snapshots taken so far:

| File (data/vast-backup/) | Taken | Contents | sha256 |
|---|---|---|---|
| `vast-backup-20260926T232506Z.tar.gz` | 2026-09-27 04:55 IST, instance 52720078 | DB dump (9 MB, users incl. admin, all uploads' facts, catalogue, agent runs) + 50 scan objects | `781a44af8678213b1366aa8a59e83f4ac7d7732910020fb17e908b4883fb4275` |
| **`vast-backup-20260926T233828Z.tar.gz`** (use this one) | 2026-09-27 05:11 IST, the last state of instance 52720078 before it was destroyed | the same plus the medication agent's runs (two dumps; `cdi.dump` is the newest) | `ecb5961af15410a5357c15bae712d5aefda928410a022dbcc1c8ba96cecd2da3` |
