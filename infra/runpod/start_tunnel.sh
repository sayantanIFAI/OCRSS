#!/usr/bin/env bash
# Open (or keep) the reverse tunnel from this pod / host to the ingress VM (backlog E1-S9;
# runbook docs/ingress.md). The pod dials out, so RunPod reassigning its IP/port (ARCHITECTURE §3.3)
# never changes the address staff use: https://$CDI_INGRESS_HOSTNAME.
#
#   VM 127.0.0.1:$CDI_INGRESS_REMOTE_PORT  <--ssh -R--  this pod  -->  webapp 127.0.0.1:$CDI_WEBAPP_PORT
#
# Idempotent: a running tunnel is left alone. Not configured (CDI_INGRESS_SSH unset) = skip, exit 0.
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a

if [ -z "${CDI_INGRESS_SSH:-}" ]; then
  echo "ingress: not configured (CDI_INGRESS_SSH unset); no stable address for this pod"
  exit 0
fi
: "${CDI_INGRESS_HOST_KEY:?ingress: set CDI_INGRESS_HOST_KEY (printed by setup_ingress_vm.sh)}"
: "${CDI_INGRESS_HOSTNAME:?ingress: set CDI_INGRESS_HOSTNAME}"
SSH_HOST=${CDI_INGRESS_SSH#*@}
SSH_PORT=${CDI_INGRESS_SSH_PORT:-22}
REMOTE_PORT=${CDI_INGRESS_REMOTE_PORT:-18888}
LOCAL_PORT=${CDI_WEBAPP_PORT:-8888}
SECRETS=${CDI_SECRETS_DIR:-/run/cdi-secrets}
KEY_SRC=${CDI_INGRESS_KEY_DIR:-$WS/secrets/tunnel}
PIDFILE=$WS/logs/tunnel.pid
mkdir -p "$WS/logs" "$KEY_SRC"

# the pod's tunnel key: made once on the persistent volume, used from the 0600 overlay copy
# (MooseFS forces 0777 on /workspace and ssh refuses such a key; same pattern as E1-S5)
if [ ! -f "$KEY_SRC/id_ed25519" ]; then
  ssh-keygen -q -t ed25519 -N "" -C cdi-pod-tunnel -f "$KEY_SRC/id_ed25519"
  echo "ingress: new tunnel key. Add this line to CDI_INGRESS_POD_KEYS on the VM, re-run setup_ingress_vm.sh:"
  cat "$KEY_SRC/id_ed25519.pub"
fi
install -d -m 700 "$SECRETS"
install -m 600 "$KEY_SRC/id_ed25519" "$SECRETS/tunnel_id_ed25519"
# the VM's host key is pinned: the tunnel never connects to a host it cannot verify
if [ "$SSH_PORT" = 22 ]; then KH_NAME=$SSH_HOST; else KH_NAME="[$SSH_HOST]:$SSH_PORT"; fi
printf '%s %s\n' "$KH_NAME" "$CDI_INGRESS_HOST_KEY" >"$SECRETS/ingress_known_hosts"
chmod 600 "$SECRETS/ingress_known_hosts"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "ingress: tunnel already running (pid $(cat "$PIDFILE"))"
  exit 0
fi

# autossh restarts ssh whenever it exits; ServerAlive detects a dead VM / network within ~45 s,
# ExitOnForwardFailure retries while a dead pod's forward still holds the port on the VM
AUTOSSH_GATETIME=0 AUTOSSH_PIDFILE=$PIDFILE AUTOSSH_LOGFILE=$WS/logs/tunnel.log \
  setsid nohup autossh -M 0 -N \
    -i "$SECRETS/tunnel_id_ed25519" -p "$SSH_PORT" \
    -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$SECRETS/ingress_known_hosts" \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -R "127.0.0.1:$REMOTE_PORT:127.0.0.1:$LOCAL_PORT" \
    "$CDI_INGRESS_SSH" >>"$WS/logs/tunnel.log" 2>&1 </dev/null &
for _ in $(seq 10); do [ -s "$PIDFILE" ] && break; sleep 1; done
echo "ingress: tunnel started (pid $(cat "$PIDFILE" 2>/dev/null || echo '?'); log $WS/logs/tunnel.log)"
