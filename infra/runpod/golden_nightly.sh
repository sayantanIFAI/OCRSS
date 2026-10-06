#!/usr/bin/env bash
# Nightly golden-path run against the REAL model (backlog E1-S6; ARCHITECTURE §8.2).
# cron (installed by bootstrap_pod.sh) runs it at 02:30; safe to run by hand.
#
# Isolation: a fresh `cdi_golden` database and a separate `cdi-golden` bucket, so the live `cdi`
# database and `cdi-documents` bucket are never written. Drift -> exit 1 and one line in
# $WS/logs/golden_alerts.log (the alert channel until E10 observability exists); the measured
# counts are always in $WS/logs/golden_last.json.
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
export CDI_SECRETS_DIR=${CDI_SECRETS_DIR:-/run/cdi-secrets}
export CDI_S3_BUCKET=cdi-golden
PY="$REPO/.venv/bin/python"

# the model gateway must be up with the real backend, else the run would be meaningless
HEALTH=$(curl -fsS "http://127.0.0.1:${CDI_MLSERVE_PORT:-8077}/healthz" || true)
if ! grep -q '"loaded": *true' <<<"$HEALTH" || ! grep -q '"backend": *"hf"' <<<"$HEALTH"; then
  echo "golden: mlserve is not the loaded hf backend; skipped: $HEALTH"
  exit 2
fi

# the cdi role cannot CREATE DATABASE; the superuser creates it once, owned by cdi
su postgres -c "psql -h /tmp -tAc \"SELECT 1 FROM pg_database WHERE datname='cdi_golden'\"" \
  | grep -q 1 || su postgres -c "createdb -h /tmp -O cdi cdi_golden"

"$PY" -c "from cdi_adapter.storage import get_store; get_store().ensure_bucket()"
cd "$REPO"
"$PY" scripts/golden_path.py --mode full --baseline real_vlm --fresh-db cdi_golden \
  --report "$WS/logs/golden_last.json" --alert-log "$WS/logs/golden_alerts.log"
