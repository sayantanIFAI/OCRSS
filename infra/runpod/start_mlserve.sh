#!/usr/bin/env bash
# Start the model gateway on localhost:8077 (ARCHITECTURE §3.4). The model lazy-loads on the first
# request (~35 s); weights are cached in /workspace/hf-cache.
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
PORT=${CDI_MLSERVE_PORT:-8077}
export HF_HOME=${HF_HOME:-$WS/hf-cache}

health_matches() {
  python3 -c 'import json,sys
h=json.load(sys.stdin)
sys.exit(0 if h.get("status")=="ok" and h.get("backend")==sys.argv[1] else 1)' \
    "${CDI_MLSERVE_BACKEND:-stub}"
}
if HEALTH=$(curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null); then
  if health_matches <<<"$HEALTH"; then
    echo "mlserve already running with the requested backend"; exit 0
  fi
  echo "mlserve backend mismatch: stop the existing gateway and rerun start_mlserve.sh"
  exit 1
fi
# the vllm backend runs from its own venv (backlog E2-S1: vLLM on torch 2.8 needs transformers < 5)
PY=$REPO/.venv/bin/python
if [ "${CDI_MLSERVE_BACKEND:-stub}" = "vllm" ]; then PY=$REPO/.venv-vllm/bin/python; fi
# 8-bit weights on a test host below the 24 GB bar (CDI_VLM_QUANTIZE=8bit): the pinned
# bitsandbytes, hash-checked, never resolved
if [ "${CDI_VLM_QUANTIZE:-}" = "8bit" ] && ! "$PY" -c "import bitsandbytes" 2>/dev/null; then
  . "$REPO/infra/runpod/versions.env"
  "$PY" -m pip install -q --no-deps --require-hashes -r <(
    echo "bitsandbytes==$BITSANDBYTES_VERSION --hash=sha256:$BITSANDBYTES_SHA256")
fi
# Background the command itself, not a `cd && ...` list: a backgrounded list runs in a subshell that
# keeps this script's stdout open for the server's lifetime, so a caller reading our output (a
# notebook cell, `$(...)`, a pipe) never sees EOF and hangs.
cd "$REPO"
setsid nohup "$PY" -m cdi_adapter.mlserve >"$WS/logs/mlserve.log" 2>&1 </dev/null &
for _ in $(seq 60); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null | health_matches && { echo "mlserve up"; exit 0; }
  sleep 1
done
echo "mlserve did not start; see $WS/logs/mlserve.log"; exit 1
