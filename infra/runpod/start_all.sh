#!/usr/bin/env bash
# One-command, idempotent lifecycle for the pod or a fallback host (ARCHITECTURE §3.3, §10.1-§10.2;
# backlog E1-S3):
#   bootstrap_pod.sh -> start_mlserve.sh -> webapp (:8888) -> print the live URL.
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/lib.sh"

bash "$HERE/bootstrap_pod.sh"
bash "$HERE/start_mlserve.sh"

set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
PORT=${CDI_WEBAPP_PORT:-8888}
if ! curl -fsS "http://127.0.0.1:$PORT/healthz" | grep -q '"db"'; then
  ( cd "$REPO" && setsid nohup "$REPO/.venv/bin/python" -m cdi_adapter.webapp \
      >"$WS/logs/webapp.log" 2>&1 </dev/null & )
  # RunPod's proxy answers HTTP 200 with a fake 502 page for unbound ports (§10.4): check a
  # content marker, never just the status code.
  for _ in $(seq 90); do
    curl -fsS "http://127.0.0.1:$PORT/" 2>/dev/null | grep -q "Agentic EMR generation" && break
    sleep 1
  done
fi
curl -fsS "http://127.0.0.1:$PORT/" | grep -q "Agentic EMR generation" \
  || { echo "webapp did not come up; see $WS/logs/webapp.log"; exit 1; }
echo "LIVE URL: $(live_url "$PORT")"
if [ "$(host_kind)" = fallback ]; then
  # the web UI has no login yet (ARCHITECTURE §12, backlog E11-S1) and binds 0.0.0.0
  echo "WARNING: fallback host - allow port $PORT from the clinic network / VPN only (docs/fallback-host.md)"
fi
