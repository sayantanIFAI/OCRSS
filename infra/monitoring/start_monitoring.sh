#!/usr/bin/env bash
# Prometheus + Alertmanager + Grafana for CDI-Adapter (backlog E10-S2). Native processes, no Docker
# (ARCHITECTURE §3.3); every version and sha256 is pinned in infra/runpod/versions.env; everything
# listens on 127.0.0.1 only (§12). Idempotent: installs what is missing, starts what is down.
#
#   bash infra/monitoring/start_monitoring.sh          # after start_all.sh
#
# Reach the UIs from a workstation over SSH only, e.g. ssh -L 3000:127.0.0.1:3000 <pod>.
# Grafana admin password: python3 src/cdi_adapter/secretstore.py get cdi_grafana_admin_password
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
HERE=$(cd "$(dirname "$0")" && pwd)
. "$REPO/infra/runpod/versions.env"
TOOLS=$WS/tools
DATA=$WS/monitoring
LOGS=$WS/logs
mkdir -p "$TOOLS" "$DATA/prometheus" "$DATA/alertmanager" "$DATA/grafana" "$LOGS"

# ---------------------------------------------------------------- install (pinned + sha256)
fetch() {  # fetch <dir-name> <url> <sha256>: download, verify, unpack into $TOOLS/<dir-name>
  local name=$1 url=$2 sha=$3 arc=$TOOLS/$1.archive un=$TOOLS/$1.unpack
  [ -d "$TOOLS/$name" ] && return 0
  curl -fsSL -o "$arc.partial" "$url"
  echo "$sha  $arc.partial" | sha256sum -c --quiet - \
    || { rm -f "$arc.partial"; echo "sha256 mismatch for $url"; exit 1; }
  mv "$arc.partial" "$arc"
  rm -rf "$un" && mkdir -p "$un"
  case "$url" in  # Loki ships a zip; python3 unpacks it, so no unpinned `unzip` is needed
    *.zip) python3 -m zipfile -e "$arc" "$un" ;;
    *)     tar -xzf "$arc" -C "$un" ;;
  esac
  # one top-level folder (Prometheus, Grafana) -> its contents; flat archives (Tempo, Loki) as is
  if [ "$(ls -A "$un" | wc -l)" = 1 ] && [ -d "$un/$(ls -A "$un")" ]; then
    mv "$un/$(ls -A "$un")" "$TOOLS/$name" && rmdir "$un"
  else
    mv "$un" "$TOOLS/$name"
  fi
  chmod -R u+x "$TOOLS/$name" 2>/dev/null || true   # zip does not keep the exec bit
  rm -f "$arc"
}
PROM=prometheus-$PROMETHEUS_VERSION
AM=alertmanager-$ALERTMANAGER_VERSION
GRAF=grafana-$GRAFANA_VERSION
TEMPO=tempo-$TEMPO_VERSION
LOKI=loki-$LOKI_VERSION
fetch "$PROM" "https://github.com/prometheus/prometheus/releases/download/v$PROMETHEUS_VERSION/$PROM.linux-amd64.tar.gz" "$PROMETHEUS_SHA256"
fetch "$AM" "https://github.com/prometheus/alertmanager/releases/download/v$ALERTMANAGER_VERSION/$AM.linux-amd64.tar.gz" "$ALERTMANAGER_SHA256"
fetch "$GRAF" "https://dl.grafana.com/oss/release/$GRAF.linux-amd64.tar.gz" "$GRAFANA_SHA256"
fetch "$TEMPO" "https://github.com/grafana/tempo/releases/download/v$TEMPO_VERSION/tempo_${TEMPO_VERSION}_linux_amd64.tar.gz" "$TEMPO_SHA256"
fetch "$LOKI" "https://github.com/grafana/loki/releases/download/v$LOKI_VERSION/loki-linux-amd64.zip" "$LOKI_SHA256"
mkdir -p "$DATA/tempo" "$DATA/loki"

# the rules must load before Prometheus is (re)started with them
"$TOOLS/$PROM/promtool" check config "$HERE/prometheus.yml"
"$TOOLS/$AM/amtool" check-config "$HERE/alertmanager.yml"

# ---------------------------------------------------------------- Grafana admin password
# from the secrets store (backlog E1-S5): created once on the persistent copy, read from the
# private overlay copy, put in a 0600 file with no trailing newline. Grafana 13.2 ignores
# the admin-password __FILE env override and silently creates admin/admin (found on the Colab deploy
# check, 2026-09-24), so the password is SET after start with `grafana cli admin
# reset-admin-password --password-from-stdin`, and the start fails if admin/admin still works.
SECRETS_PY="$REPO/src/cdi_adapter/secretstore.py"
SECRETS_SRC=${SECRETS_SRC:-$WS/secrets}
export CDI_SECRETS_DIR=${CDI_SECRETS_DIR:-/run/cdi-secrets}
python3 "$SECRETS_PY" --dir "$SECRETS_SRC" init >/dev/null
install -d -m 700 "$CDI_SECRETS_DIR"
install -m 600 "$SECRETS_SRC/cdi_grafana_admin_password" "$CDI_SECRETS_DIR/"
GF_PW_FILE=$CDI_SECRETS_DIR/grafana_admin_password.raw
( umask 077; printf '%s' "$(python3 "$SECRETS_PY" --dir "$CDI_SECRETS_DIR" get cdi_grafana_admin_password)" > "$GF_PW_FILE" )

# ---------------------------------------------------------------- start
up() { curl -fsS -m 3 "$1" >/dev/null 2>&1; }
wait_up() {  # wait_up <name> <url>
  for _ in $(seq 60); do up "$2" && { echo "$1 up"; return 0; }; sleep 1; done
  echo "$1 did not start; see $LOGS/$1.log"; exit 1
}

# already running (an upgrade): SIGHUP makes it re-read its config and rules; a process only reads
# them at start, so without this a new or changed alert rule would never load (E10-S4)
reload() { pkill -HUP -f "$1" && echo "$2 reloaded its configuration" || true; }

if up http://127.0.0.1:9093/-/ready; then
  reload "$TOOLS/$AM/alertmanager --config.file" alertmanager
else
  setsid nohup "$TOOLS/$AM/alertmanager" --config.file="$HERE/alertmanager.yml" \
    --storage.path="$DATA/alertmanager" --web.listen-address=127.0.0.1:9093 \
    --cluster.listen-address= >"$LOGS/alertmanager.log" 2>&1 </dev/null &
fi
wait_up alertmanager http://127.0.0.1:9093/-/ready

if up http://127.0.0.1:9090/-/ready; then
  reload "$TOOLS/$PROM/prometheus --config.file" prometheus
else
  setsid nohup "$TOOLS/$PROM/prometheus" --config.file="$HERE/prometheus.yml" \
    --storage.tsdb.path="$DATA/prometheus" --storage.tsdb.retention.time=30d \
    --web.listen-address=127.0.0.1:9090 >"$LOGS/prometheus.log" 2>&1 </dev/null &
fi
wait_up prometheus http://127.0.0.1:9090/-/ready

# traces and logs (E10-S3): OTLP/HTTP from the services, loopback only
if ! up http://127.0.0.1:3200/ready; then
  TEMPO_DATA="$DATA/tempo" setsid nohup "$TOOLS/$TEMPO/tempo" -config.file="$HERE/tempo.yaml" \
    -config.expand-env=true >"$LOGS/tempo.log" 2>&1 </dev/null &
fi
wait_up tempo http://127.0.0.1:3200/ready

if ! up http://127.0.0.1:3100/ready; then
  LOKI_DATA="$DATA/loki" setsid nohup "$TOOLS/$LOKI/loki-linux-amd64" -config.file="$HERE/loki.yaml" \
    -config.expand-env=true >"$LOGS/loki.log" 2>&1 </dev/null &
fi
wait_up loki http://127.0.0.1:3100/ready

if ! up http://127.0.0.1:3000/api/health; then
  ( export GF_SERVER_HTTP_ADDR=127.0.0.1 GF_SERVER_HTTP_PORT=3000 \
      GF_PATHS_DATA="$DATA/grafana" GF_PATHS_LOGS="$LOGS" \
      GF_PATHS_PROVISIONING="$HERE/grafana/provisioning" CDI_DASHBOARDS="$HERE/grafana/dashboards" \
      GF_SECURITY_ADMIN_USER=admin \
      GF_AUTH_ANONYMOUS_ENABLED=false GF_USERS_ALLOW_SIGN_UP=false \
      GF_ANALYTICS_REPORTING_ENABLED=false GF_ANALYTICS_CHECK_FOR_UPDATES=false \
      GF_PLUGINS_PREINSTALL_DISABLED=true
    cd "$TOOLS/$GRAF" && setsid nohup ./bin/grafana server --homepath "$TOOLS/$GRAF" \
      >"$LOGS/grafana.log" 2>&1 </dev/null & )
fi
wait_up grafana http://127.0.0.1:3000/api/health

# the admin password is the store's, on every start (also rotates it after a store rotation)
( cd "$TOOLS/$GRAF" && GF_PATHS_DATA="$DATA/grafana" GF_PATHS_LOGS="$LOGS" \
    ./bin/grafana cli --homepath "$TOOLS/$GRAF" admin reset-admin-password --password-from-stdin \
    <"$GF_PW_FILE" >>"$LOGS/grafana-cli.log" 2>&1 )
# credentials go to curl on stdin (-K -), never on its command line
code() { printf 'user = "admin:%s"\n' "$1" \
  | curl -s -o /dev/null -w '%{http_code}' -K - http://127.0.0.1:3000/api/org; }
# the store's password first: Grafana locks `admin` for 5 minutes after 5 failed sign-ins, and
# the default check below is one deliberate failure
[ "$(code "$(cat "$GF_PW_FILE")")" = 200 ] \
  || { echo "Grafana refuses the store's admin password (or admin is locked after failed" \
            "sign-ins: wait 5 minutes); see $LOGS/grafana-cli.log"; exit 1; }
[ "$(code admin)" = 401 ] \
  || { echo "Grafana still accepts admin/admin: refusing to leave it running"; pkill -f "grafana server"; exit 1; }
echo "grafana admin password: from the secrets store (admin/admin refused)"

# Grafana reads provisioning only at start: a Grafana already running from an earlier version of
# this repo would not know newer data sources or dashboards (found on the Colab check, E10-S3)
for what in datasources dashboards; do
  printf 'user = "admin:%s"\n' "$(cat "$GF_PW_FILE")" \
    | curl -fsS -o /dev/null -X POST -K - "http://127.0.0.1:3000/api/admin/provisioning/$what/reload" \
    || { echo "Grafana did not reload its $what provisioning"; exit 1; }
done
echo "monitoring up: prometheus 127.0.0.1:9090, alertmanager 127.0.0.1:9093, grafana 127.0.0.1:3000,"
echo "               tempo 127.0.0.1:3200 (OTLP 4318), loki 127.0.0.1:3100"
