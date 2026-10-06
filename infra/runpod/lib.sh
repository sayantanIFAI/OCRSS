# Shared helpers for the native lifecycle scripts (ARCHITECTURE §3.3). Sourced, not executed.
# The same scripts run on the RunPod pod and on a non-RunPod fallback host (backlog E1-S3,
# owner decision 2026-09-21: native processes, no Docker).

# "runpod" when RunPod injected its pod id, else "fallback"
host_kind() {
  if [ -n "${RUNPOD_POD_ID:-}" ]; then echo runpod; else echo fallback; fi
}

# the address staff open: RunPod's TLS proxy on the pod; the host's own address on a fallback
# host (a stable hostname + TLS in front of either is E1-S9)
live_url() {
  local port=$1
  if [ "$(host_kind)" = runpod ]; then
    echo "https://${RUNPOD_POD_ID}-${port}.proxy.runpod.net"
  else
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "http://${CDI_PUBLIC_HOST:-${ip:-127.0.0.1}}:${port}"
  fi
}
