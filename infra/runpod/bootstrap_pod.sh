#!/usr/bin/env bash
# Idempotent pod bootstrap (ARCHITECTURE §3.3, §10.1-§10.2). Native processes, NO Docker.
#
#   overlay (wiped on restart): apt packages, PGDATA=/var/lib/postgresql/16/cdi
#   /workspace (persistent):    repo + .venv, hf-cache, minio-data, redis AOF, backup/cdi.dump, logs
#
# Postgres lives on the ephemeral overlay because /workspace (MooseFS) forces 0777 and rejects
# chown, which Postgres refuses for PGDATA. It is rebuilt and pg_restore'd from the 15-minute dump.
set -euo pipefail

WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
LOG=$WS/logs/bootstrap.log
PGDATA=${PGDATA:-/var/lib/postgresql/16/cdi}
PGBIN=/usr/lib/postgresql/16/bin
DUMP=$WS/backup/cdi.dump
GO_VERSION=${GO_VERSION:-1.27.1}
GO_SHA256=${GO_SHA256:-}                      # set to go.dev's published sha256 for linux-amd64
MINIO_TAG=${MINIO_TAG:-RELEASE.2025-10-15T17-29-55Z}
MINIO_BIN=$WS/tools/minio-$MINIO_TAG

mkdir -p "$WS/logs" "$WS/backup" "$WS/redis" "$WS/minio-data" "$WS/tools" "$WS/hf-cache" \
         "$WS/data/inbox" "$WS/data/processed" "$WS/data/failed"
exec > >(tee -a "$LOG") 2>&1
echo "=== bootstrap $(date -Is) ==="

set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a

# ---------------------------------------------------------------- 0. secrets store (backlog E1-S5)
# The persistent copy lives on /workspace, whose MooseFS forces 0777 (docs/secrets.md); every
# service reads the 0700/0600 copy on the overlay. secretstore.py is stdlib-only (no venv needed).
SECRETS_PY="$REPO/src/cdi_adapter/secretstore.py"
SECRETS_SRC=${SECRETS_SRC:-$WS/secrets}
export CDI_SECRETS_DIR=${CDI_SECRETS_DIR:-/run/cdi-secrets}
secret() { python3 "$SECRETS_PY" --dir "$CDI_SECRETS_DIR" "$@"; }
python3 "$SECRETS_PY" --dir "$SECRETS_SRC" init          # first boot only; never overwrites
install -d -m 700 "$CDI_SECRETS_DIR"
for f in "$SECRETS_SRC"/cdi_*; do install -m 600 "$f" "$CDI_SECRETS_DIR/"; done
secret check                                             # refuses dev defaults and open modes
secret pgpass "$CDI_SECRETS_DIR/pgpass"
secret redis-conf "$CDI_SECRETS_DIR/redis.conf"
export PGPASSFILE="$CDI_SECRETS_DIR/pgpass"                # libpq: no password on any command line
REDISCLI_AUTH=$(secret get cdi_redis_url --part password)
export REDISCLI_AUTH
PSQL_URL=postgresql://cdi@127.0.0.1:5432/cdi              # password comes from PGPASSFILE

# ---------------------------------------------------------------- 1. apt (reinstalled after restart)
if ! command -v "$PGBIN/postgres" >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq postgresql-16 postgresql-client-16 redis-server cron git curl \
    libgl1 libglib2.0-0 python3-venv
fi

# ---------------------------------------------------------------- 2. Postgres on the overlay
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  install -d -o postgres -g postgres -m 700 "$PGDATA"
  su postgres -c "$PGBIN/initdb -D $PGDATA -E UTF8 --locale=C.UTF-8"
fi
if ! su postgres -c "$PGBIN/pg_ctl -D $PGDATA status" >/dev/null 2>&1; then
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l $WS/logs/postgres.log -o '-p 5432 -k /tmp -c listen_addresses=127.0.0.1' -w start"
fi
# the role password is the store's, re-applied every boot, passed on stdin (never argv)
DB_PW=$(secret get cdi_database_url --part password)
su postgres -c "psql -h /tmp -v ON_ERROR_STOP=1 -q" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cdi') THEN CREATE ROLE cdi LOGIN; END IF;
END \$\$;
ALTER ROLE cdi PASSWORD '$DB_PW';
SQL
unset DB_PW
su postgres -c "psql -h /tmp -tAc \"SELECT 1 FROM pg_database WHERE datname='cdi'\"" | grep -q 1 || {
  su postgres -c "psql -h /tmp -c 'CREATE DATABASE cdi OWNER cdi'"
  if [ -s "$DUMP" ]; then
    echo "restoring $DUMP"
    pg_restore --no-owner --role=cdi -d "$PSQL_URL" "$DUMP" || echo "WARN: pg_restore reported errors"
  fi
}

# ---------------------------------------------------------------- 3. venv (torch inherited)
if [ ! -x "$REPO/.venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$REPO/.venv"
fi
"$REPO/.venv/bin/python" -m pip install -q --upgrade pip
"$REPO/.venv/bin/python" -m pip install -q -e "$REPO[ocr,ml]"
# RunPod images ship torch, inherited above (Appendix B); a non-RunPod fallback host (E1-S3) does
# not, so the hf backend gets the same pinned CUDA 12.8 build there. Stub hosts need no torch.
TORCH_VERSION=${TORCH_VERSION:-2.8.0}
TORCHVISION_VERSION=${TORCHVISION_VERSION:-0.23.0}
if [ "${CDI_MLSERVE_BACKEND:-stub}" = "hf" ] && ! "$REPO/.venv/bin/python" -c "import torch" 2>/dev/null; then
  "$REPO/.venv/bin/python" -m pip install -q "torch==$TORCH_VERSION" \
    "torchvision==$TORCHVISION_VERSION" --index-url https://download.pytorch.org/whl/cu128
fi
( cd "$REPO" && "$REPO/.venv/bin/python" -m alembic upgrade head )

# ---------------------------------------------------------------- 4. Redis (AOF on /workspace)
if ! redis-cli -p 6379 ping >/dev/null 2>&1; then
  # requirepass comes from the store's include file, not the command line
  redis-server "$CDI_SECRETS_DIR/redis.conf" --port 6379 --bind 127.0.0.1 --dir "$WS/redis" \
    --appendonly yes --daemonize yes --logfile "$WS/logs/redis.log"
fi

# ---------------------------------------------------------------- 5. MinIO, built from source
# MinIO no longer publishes binaries (dl.min.io returns 410; releases carry no assets), so the
# pinned release tag is built from the official repository with the official Go toolchain.
if [ ! -x "$MINIO_BIN" ]; then
  if [ ! -x "$WS/tools/go/bin/go" ]; then
    curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz"
    if [ -n "$GO_SHA256" ]; then echo "$GO_SHA256  /tmp/go.tgz" | sha256sum -c -; fi
    tar -C "$WS/tools" -xzf /tmp/go.tgz && rm /tmp/go.tgz
  fi
  rm -rf /tmp/minio-src
  git clone --depth 1 --branch "$MINIO_TAG" https://github.com/minio/minio.git /tmp/minio-src
  ( cd /tmp/minio-src && GOFLAGS=-trimpath "$WS/tools/go/bin/go" build -o "$MINIO_BIN" . )
  rm -rf /tmp/minio-src
fi
if ! curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
  MINIO_ROOT_USER=$(secret get cdi_s3_access_key) MINIO_ROOT_PASSWORD=$(secret get cdi_s3_secret_key) \
    setsid nohup "$MINIO_BIN" server "$WS/minio-data" --address 127.0.0.1:9000 \
      --console-address 127.0.0.1:9001 >"$WS/logs/minio.log" 2>&1 </dev/null &
  for _ in $(seq 60); do curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 && break; sleep 1; done
fi
"$REPO/.venv/bin/python" -c "from cdi_adapter.storage import get_store; get_store().ensure_bucket()"

# ---------------------------------------------------------------- 6. cron: DB snapshot every 15 min
service cron start >/dev/null 2>&1 || cron || true
( crontab -l 2>/dev/null | grep -v -e 'infra/runpod/snapshot.sh' -e 'infra/runpod/golden_nightly.sh'
  echo "*/15 * * * * bash $REPO/infra/runpod/snapshot.sh >>$WS/logs/snapshot.log 2>&1"
  # E1-S6: nightly golden path on the real model (isolated DB + bucket; drift -> golden_alerts.log)
  echo "30 2 * * * bash $REPO/infra/runpod/golden_nightly.sh >>$WS/logs/golden.log 2>&1" ) | crontab -

echo "=== bootstrap done $(date -Is) ==="
