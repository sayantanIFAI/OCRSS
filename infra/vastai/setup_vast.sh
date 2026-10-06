#!/usr/bin/env bash
# One-command bring-up of the whole stack on a fresh (or restarted) vast.ai GPU instance
# (docs/vast-ai.md). Run ON the instance, after scripts/dev/vast_deploy.sh has copied the files:
#
#   /workspace/cdi-src.tar.gz            the verified source bundle (scripts/dev/colab_bundle.py)
#   /workspace/catalogue.csv             the medicine catalogue snapshot (optional; git-ignored data)
#   /workspace/vast-backup.tar.gz        a snapshot to restore (optional; infra/runpod/snapshot.sh)
#
# A vast.ai instance is a VERIFICATION host: outside India, so test / de-identified data only.
# Steps, each safe to run again:
#   1. unpack the bundle into /workspace/cdi (sync.sh when a tree is already there)
#   2. .env from the pod template (hf backend: the full bf16 Qwen2.5-VL-7B needs >= 24 GB VRAM)
#   3. a backup, if given, is put in /workspace/backup BEFORE the bootstrap: the bootstrap restores
#      /workspace/backup/cdi.dump into its new cluster (docs/dr.md); the scans are restored after
#   4. bootstrap_pod.sh (apt pins, PostgreSQL 16, venv, Redis, MinIO, migrations). It returns 1 on
#      the known FHIR package digest drift (infra/colab/colab_setup.sh header); the services are
#      checked explicitly instead
#   5. model gateway, a warm-up request (the 7B loads, ~20 s after the first download), app tier
#   6. the medicine catalogue, when given and not yet loaded
#   7. the public test link: a Cloudflare quick tunnel, retried while Cloudflare answers 429
set -uo pipefail

WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
export WS REPO
step() { printf '\n=== %s\n' "$1"; }
fail() { echo "SETUP FAILED: $1"; exit 1; }

step "1. source"
mkdir -p "$WS/logs"
if [ -f "$REPO/.env" ] && [ -d "$REPO/src" ]; then
  tar -xzf "$WS/cdi-src.tar.gz" -C /tmp infra/colab/sync.sh
  # sync.sh prints SYNCED when the tree is applied (its exit status is not a reliable signal)
  bash /tmp/infra/colab/sync.sh "$WS/cdi-src.tar.gz" 2>&1 | tee "$WS/logs/sync.log"
  grep -q '^SYNCED' "$WS/logs/sync.log" || fail "sync (see $WS/logs/sync.log)"
  FRESH=0
else
  mkdir -p "$REPO" && tar -xzf "$WS/cdi-src.tar.gz" -C "$REPO" || fail "unpack"
  FRESH=1
fi
cd "$REPO"

step "2. .env"
[ -f .env ] || cp .env.runpod .env
sed -i 's/^CDI_MLSERVE_BACKEND=stub$/CDI_MLSERVE_BACKEND=hf/' .env
grep -e '^CDI_MLSERVE_BACKEND' -e '^CDI_VLM_QUANTIZE' .env || true
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || fail "no NVIDIA GPU"

step "3. backup to restore"
RESTORE=0
if [ -f "$WS/vast-backup.tar.gz" ] && [ ! -f "$WS/backup/.restored" ]; then
  tar -xzf "$WS/vast-backup.tar.gz" -C "$WS" && RESTORE=1 && echo "backup unpacked into $WS/backup"
else
  echo "none (or already restored)"
fi

step "4. bootstrap (Postgres, Redis, MinIO, venv, migrations)"
if [ "$FRESH" = 1 ] || ! pg_isready -h 127.0.0.1 -q; then
  bash infra/runpod/bootstrap_pod.sh >"$WS/logs/bootstrap.log" 2>&1 \
    || echo "bootstrap returned $? (FHIR drift is tolerated) - services checked below"
fi
pg_isready -h 127.0.0.1 -q && echo "postgres OK" || fail "postgres down (see $WS/logs/bootstrap.log)"
curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null && echo "minio    OK" || fail "minio down"
( set -a; . ./.env; set +a; .venv/bin/python -m alembic upgrade head ) >/dev/null 2>&1 \
  && echo "schema   at head" || fail "migrations"
if [ "$RESTORE" = 1 ]; then
  ( set -a; . ./.env; set +a; .venv/bin/python -c "
from pathlib import Path
from cdi_adapter.dr.drill import restore_objects
from cdi_adapter.storage import get_store
print('scans restored:', restore_objects(Path('$WS/backup'), get_store()))" ) || fail "scan restore"
  touch "$WS/backup/.restored"
fi

step "5. model gateway + app tier"
bash infra/runpod/start_mlserve.sh || fail "mlserve (see $WS/logs/mlserve.log)"
curl -s -m 900 -X POST http://127.0.0.1:8077/vlm/generate -H 'content-type: application/json' \
  -d '{"prompt":"Say OK","max_new_tokens":4}' | head -c 160; echo
bash infra/runpod/start_apptier.sh | tail -1 || fail "app tier"
curl -fsS http://127.0.0.1:8888/healthz | grep -q '"status":"ok"' \
  && echo "website  OK" || fail "website not healthy: $(curl -s http://127.0.0.1:8888/healthz)"

step "6. medicine catalogue"
( set -a; . ./.env; set +a
  n=$(.venv/bin/python -m cdi_adapter.catalogue stats 2>/dev/null | grep -o '"products": [0-9]*' | grep -o '[0-9]*')
  if [ "${n:-0}" -gt 0 ]; then echo "already loaded: $n products"
  elif [ -f "$WS/catalogue.csv" ]; then
    mkdir -p data/catalogue/tata1mg && cp "$WS/catalogue.csv" data/catalogue/tata1mg/indian_medicine_data.csv
    .venv/bin/python -m cdi_adapter.catalogue import --file data/catalogue/tata1mg/indian_medicine_data.csv \
      | grep -e '"rows"' -e sha256
  else echo "no catalogue file given: the catalogue layer and the medication agent stay off"; fi )

step "7. public test link"
pkill -f 'infra/vastai/tunnel_retry[.]sh' 2>/dev/null || true
setsid nohup bash infra/vastai/tunnel_retry.sh >"$WS/logs/tunnel_retry.log" 2>&1 </dev/null &
sleep 20
grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$WS/logs/tunnel_retry.log" | tail -1 \
  || echo "not granted yet (Cloudflare 429): retrying every 2 min; watch $WS/logs/tunnel_retry.log"
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
echo "SETUP OK"
