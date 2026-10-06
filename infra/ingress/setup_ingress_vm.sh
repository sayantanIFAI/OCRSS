#!/usr/bin/env bash
# Idempotent setup of the ingress VM (backlog E1-S9; ARCHITECTURE §3.3, §12; runbook docs/ingress.md).
# Native processes, NO Docker. Run as root on an Ubuntu 24.04 VM in an India region:
#
#   sudo bash infra/ingress/setup_ingress_vm.sh /etc/cdi-ingress.env
#
# The env file (template: infra/ingress/cdi-ingress.env.example) sets:
#   CDI_INGRESS_HOSTNAME     stable hostname staff open; its DNS A/AAAA record points at this VM
#   CDI_INGRESS_ACME_EMAIL   Let's Encrypt account e-mail (expiry notices)
#   CDI_INGRESS_ALLOW        clinic network CIDRs allowed to reach the UI (space-separated)
#   CDI_INGRESS_POD_KEYS     the pods' tunnel public keys (one per line); start_tunnel.sh prints them
#   CDI_INGRESS_REMOTE_PORT  VM-local port the tunnel listens on (default 18888)
#
# Safe to re-run: it converges to the same state (a new pod key or allowlist = edit, re-run).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
TUNNEL_USER=cdi-tunnel

# the authorized_keys line for one pod key: port forwarding to one VM-local port and nothing else
authorized_keys_line() {
  local port=$1 key=$2
  printf 'restrict,port-forwarding,permitlisten="127.0.0.1:%s",command="/usr/sbin/nologin" %s\n' \
    "$port" "$key"
}

# refuse a configuration that would publish the UI or break TLS
check_config() {
  local ok=0
  [ -n "${CDI_INGRESS_HOSTNAME:-}" ] || { echo "CDI_INGRESS_HOSTNAME is not set"; ok=1; }
  [ -n "${CDI_INGRESS_ACME_EMAIL:-}" ] || { echo "CDI_INGRESS_ACME_EMAIL is not set"; ok=1; }
  if [ -z "${CDI_INGRESS_ALLOW:-}" ]; then
    echo "CDI_INGRESS_ALLOW is empty: the UI holds patient data, so it is never public"; ok=1
  fi
  local cidr
  for cidr in ${CDI_INGRESS_ALLOW:-}; do
    case "$cidr" in
      0.0.0.0/0 | ::/0) echo "CDI_INGRESS_ALLOW may not contain $cidr (that is everyone)"; ok=1 ;;
    esac
  done
  [ -n "${CDI_INGRESS_POD_KEYS:-}" ] || { echo "CDI_INGRESS_POD_KEYS is empty: no pod can connect"; ok=1; }
  [[ "${CDI_INGRESS_REMOTE_PORT}" =~ ^[0-9]+$ ]] || { echo "CDI_INGRESS_REMOTE_PORT is not a port"; ok=1; }
  return "$ok"
}

main() {
  local envfile=${1:?usage: setup_ingress_vm.sh /etc/cdi-ingress.env}
  set -a; . "$envfile"; set +a
  CDI_INGRESS_REMOTE_PORT=${CDI_INGRESS_REMOTE_PORT:-18888}
  . "$HERE/versions.env"
  check_config

  # ------------------------------------------------------------ 1. Caddy, pinned + checksum-verified
  if [ "$(dpkg-query -W -f='${Version}' caddy 2>/dev/null)" != "$CADDY_VERSION" ]; then
    local deb=/tmp/caddy_${CADDY_VERSION}_linux_amd64.deb
    curl -fsSL -o "$deb" \
      "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.deb"
    echo "$CADDY_SHA512  $deb" | sha512sum -c -
    dpkg -i "$deb"
    rm -f "$deb"
  fi

  # ------------------------------------------------------------ 2. tunnel user: forwarding only
  if ! id "$TUNNEL_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$TUNNEL_USER"
  fi
  local home
  home=$(getent passwd "$TUNNEL_USER" | cut -d: -f6)
  install -d -m 700 -o "$TUNNEL_USER" -g "$TUNNEL_USER" "$home/.ssh"
  local key
  while IFS= read -r key; do
    [ -n "$key" ] && authorized_keys_line "$CDI_INGRESS_REMOTE_PORT" "$key"
  done <<<"$CDI_INGRESS_POD_KEYS" >"$home/.ssh/authorized_keys.new"
  install -m 600 -o "$TUNNEL_USER" -g "$TUNNEL_USER" "$home/.ssh/authorized_keys.new" \
    "$home/.ssh/authorized_keys"
  rm -f "$home/.ssh/authorized_keys.new"

  # a dead pod's forward is dropped within ~45 s, so the replacement pod can take the port
  cat >/etc/ssh/sshd_config.d/60-cdi-tunnel.conf <<EOF
# backlog E1-S9: the pod's reverse tunnel. Written by setup_ingress_vm.sh.
Match User $TUNNEL_USER
    AuthenticationMethods publickey
    AllowTcpForwarding remote
    GatewayPorts no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PermitTunnel no
    ClientAliveInterval 15
    ClientAliveCountMax 3
EOF
  sshd -t
  # 24.04 socket-activates sshd: reload a running daemon, else the next start reads the config
  systemctl try-reload-or-restart ssh

  # ------------------------------------------------------------ 3. Caddy config, validated first
  umask 077
  cat >/etc/caddy/cdi.env <<EOF
CDI_INGRESS_HOSTNAME="$CDI_INGRESS_HOSTNAME"
CDI_INGRESS_ACME_EMAIL="$CDI_INGRESS_ACME_EMAIL"
CDI_INGRESS_ALLOW="$CDI_INGRESS_ALLOW"
CDI_INGRESS_REMOTE_PORT="$CDI_INGRESS_REMOTE_PORT"
EOF
  umask 022
  install -m 644 "$HERE/Caddyfile" /etc/caddy/Caddyfile.new
  # the same values systemd gives the service (quoted: the allowlist has spaces)
  ( set -a; . /etc/caddy/cdi.env; set +a
    caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new )
  mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
  install -d /etc/systemd/system/caddy.service.d
  cat >/etc/systemd/system/caddy.service.d/cdi.conf <<EOF
[Service]
EnvironmentFile=/etc/caddy/cdi.env
EOF
  systemctl daemon-reload
  systemctl enable caddy >/dev/null
  if systemctl is-active --quiet caddy; then systemctl reload caddy; else systemctl start caddy; fi

  # ------------------------------------------------------------ 4. firewall: ssh + 80/443 only
  # 80/443 stay open to everyone so Let's Encrypt can validate; the allowlist is enforced in Caddy.
  if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw --force enable >/dev/null
  fi

  echo "ingress ready: https://$CDI_INGRESS_HOSTNAME -> 127.0.0.1:$CDI_INGRESS_REMOTE_PORT (tunnel)"
  echo "set on the pod (.env):  CDI_INGRESS_HOST_KEY=\"$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)\""
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
