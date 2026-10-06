#!/usr/bin/env bash
# Idempotent pod bootstrap (ARCHITECTURE §3.3, §10.1-§10.2). Native processes, NO Docker.
# Every version comes from versions.env + requirements.lock (backlog E1-S4); step 7 records what was
# installed and stops the boot on drift from those pins.
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
HERE=$(cd "$(dirname "$0")" && pwd)
LOCK=$HERE/requirements.lock

mkdir -p "$WS/logs" "$WS/backup" "$WS/redis" "$WS/minio-data" "$WS/tools" "$WS/hf-cache" \
         "$WS/data/inbox" "$WS/data/processed" "$WS/data/failed"
exec > >(tee -a "$LOG") 2>&1
echo "=== bootstrap $(date -Is) ==="

set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
# the pins, sourced after .env so nothing in the environment can override one (backlog E1-S4)
. "$HERE/versions.env"
MINIO_BIN=$WS/tools/minio-$MINIO_TAG

# ---------------------------------------------------------------- 0. secrets store (backlog E1-S5)
# The source must enforce private permissions; every service reads a private overlay copy.
# secretstore.py is stdlib-only (no venv needed).
SECRETS_PY="$REPO/src/cdi_adapter/secretstore.py"
# Must be a private persistent mount or a vault-agent provisioned directory. Never generate
# credentials on MooseFS: its forced 0777 permissions expose them before copying to /run.
SECRETS_SRC=${SECRETS_SRC:?Set SECRETS_SRC to a private persistent or vault-provisioned directory}
export CDI_SECRETS_DIR=${CDI_SECRETS_DIR:-/run/cdi-secrets}
secret() { python3 "$SECRETS_PY" --dir "$CDI_SECRETS_DIR" "$@"; }
python3 "$SECRETS_PY" --dir "$SECRETS_SRC" init          # first boot only; fails on unsafe modes
python3 "$SECRETS_PY" --dir "$SECRETS_SRC" check
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
# Only from the Ubuntu archive frozen at UBUNTU_SNAPSHOT, at the exact APT_PINS versions, so the
# packages and every dependency they pull in are identical on each boot (backlog E1-S4).
apt_pinned() {
  local pin
  for pin in $APT_PINS; do
    [ "$(dpkg-query -W -f='${Version}' "${pin%%=*}" 2>/dev/null)" = "${pin#*=}" ] || return 1
  done
}
if ! apt_pinned; then
  export DEBIAN_FRONTEND=noninteractive
  SNAP_LIST=/etc/apt/cdi-snapshot.list
  SNAP_URL=https://snapshot.ubuntu.com/ubuntu/$UBUNTU_SNAPSHOT
  KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg
  for suite in "$UBUNTU_SERIES" "$UBUNTU_SERIES-updates" "$UBUNTU_SERIES-security"; do
    echo "deb [signed-by=$KEYRING] $SNAP_URL $suite main universe"
  done >"$SNAP_LIST"
  APT_SNAP=(-o "Dir::Etc::SourceList=$SNAP_LIST" -o "Dir::Etc::SourceParts=-"
            -o APT::Get::List-Cleanup=0)
  apt-get "${APT_SNAP[@]}" update -qq
  # shellcheck disable=SC2086  # APT_PINS is a list of pkg=version words
  apt-get "${APT_SNAP[@]}" install -y -qq --allow-downgrades $APT_PINS
fi

# ---------------------------------------------------------------- 2. Postgres on the overlay
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  install -d -o postgres -g postgres -m 700 "$PGDATA"
  su postgres -c "$PGBIN/initdb -D $PGDATA -E UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256"
fi
# E10-S6: a data folder fenced by a failover is never started as a primary again (two writable
# primaries = split brain). It comes back only as a standby: python -m cdi_adapter.ha pg rejoin
if [ -f "$PGDATA/cdi.fenced" ]; then
  echo "bootstrap stopped: $PGDATA was fenced by a failover ($(cat "$PGDATA/cdi.fenced"))."
  echo "The standby is the primary now; rejoin this one: python -m cdi_adapter.ha pg rejoin"
  exit 1
fi
if ! su postgres -c "$PGBIN/pg_ctl -D $PGDATA status" >/dev/null 2>&1; then
  # pg_ctl opens the log file as postgres. On the pod /workspace is MooseFS (0777) so any user can
  # create it; on a fallback host (E1-S3) $WS/logs is an ordinary root-owned directory, and the
  # start fails with "cannot create ...: Permission denied". Hand the file over first.
  touch "$WS/logs/postgres.log"
  chown postgres:postgres "$WS/logs/postgres.log"
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l $WS/logs/postgres.log -o '-p 5432 -k /tmp -c listen_addresses=127.0.0.1' -w start"
fi
# the role password is the store's, re-applied every boot, passed on stdin (never argv)
DB_PW=$(secret get cdi_database_url --part password)
# Quote vault-supplied apostrophes without exposing the password in process arguments.
DB_PW=${DB_PW//\'/\'\'}
su postgres -c "psql -h /tmp -v ON_ERROR_STOP=1 -q" <<SQL
SET standard_conforming_strings = on;
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cdi') THEN CREATE ROLE cdi LOGIN; END IF;
END \$\$;
ALTER ROLE cdi PASSWORD '$DB_PW';
SQL
unset DB_PW
RESTORE_MARKER=$PGDATA/cdi.restore-required
su postgres -c "psql -h /tmp -tAc \"SELECT 1 FROM pg_database WHERE datname='cdi'\"" | grep -q 1 || {
  # Record intent before CREATE: a failed or interrupted restore must be retried next boot.
  if [ -s "$DUMP" ]; then touch "$RESTORE_MARKER"; fi
  su postgres -c "psql -h /tmp -c 'CREATE DATABASE cdi OWNER cdi'"
}
if [ -f "$RESTORE_MARKER" ]; then
  [ -s "$DUMP" ] || { echo "restore required but backup is missing"; exit 1; }
  echo "restoring $DUMP"
  pg_restore --exit-on-error --single-transaction --clean --if-exists --no-owner \
    --role=cdi -d "$PSQL_URL" "$DUMP"
  rm "$RESTORE_MARKER"
fi

# ---------------------------------------------------------------- 3. venv (torch inherited)
if [ ! -x "$REPO/.venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$REPO/.venv"
fi
# exactly the locked set, every wheel hash-checked, no resolver run on the pod (backlog E1-S4);
# the project itself goes in without deps and builds with the locked setuptools/wheel
"$REPO/.venv/bin/python" -m pip install -q --require-hashes --no-deps -r "$LOCK"
"$REPO/.venv/bin/python" -m pip install -q --no-deps --no-build-isolation -e "$REPO"
# RunPod images ship torch, inherited above (Appendix B); a non-RunPod fallback host (E1-S3) does
# not, so the hf backend gets the same pinned CUDA 12.8 build there. Stub hosts need no torch.
if [ "${CDI_MLSERVE_BACKEND:-stub}" = "hf" ] && ! "$REPO/.venv/bin/python" -c "import torch" 2>/dev/null; then
  "$REPO/.venv/bin/python" -m pip install -q "torch==$TORCH_VERSION" \
    "torchvision==$TORCHVISION_VERSION" --index-url https://download.pytorch.org/whl/cu128
fi
( cd "$REPO" && "$REPO/.venv/bin/python" -m alembic upgrade head )

# ---------------------------------------------------------------- 3a. Postgres hot standby (E10-S6)
# Opt-in: CDI_PG_STANDBY_PORT (and CDI_DATABASE_HOSTS listing both servers for the app). Built from
# the primary by pg_basebackup through a replication slot; the overlay is wiped on restart, so it
# is rebuilt then. A standby on this host covers a crashed or damaged primary, not a lost pod:
# that needs it on a second host (docs/ha.md).
if [ -n "${CDI_PG_STANDBY_PORT:-}" ]; then
  export PGDATA CDI_PG_BIN=$PGBIN
  STANDBY_DATA=${CDI_PG_STANDBY_DATA:-$PGDATA-standby}
  if [ -f "$STANDBY_DATA/PG_VERSION" ]; then
    su postgres -c "$PGBIN/pg_ctl -D $STANDBY_DATA status" >/dev/null 2>&1       || su postgres -c "$PGBIN/pg_ctl -D $STANDBY_DATA -l $WS/logs/postgres-standby.log -w start"
  else
    ( cd "$REPO" && "$REPO/.venv/bin/python" -m cdi_adapter.ha pg setup-standby )
  fi
fi

# ---------------------------------------------------------------- 3b. vLLM gateway venv (E2-S1)
# Only with CDI_MLSERVE_BACKEND=vllm: vLLM 0.11.0 (torch 2.8, App. B) needs transformers < 5, so the
# model gateway runs from its own venv, pinned by requirements-vllm.lock the same way; the
# pipeline venv above is untouched. torch/torchvision come from the image (or the cu128 build).
VLLM_VENV=$REPO/.venv-vllm
if [ "${CDI_MLSERVE_BACKEND:-stub}" = "vllm" ]; then
  [ -x "$VLLM_VENV/bin/python" ] || python3 -m venv --system-site-packages "$VLLM_VENV"
  "$VLLM_VENV/bin/python" -m pip install -q --require-hashes --no-deps -r "$HERE/requirements-vllm.lock"
  "$VLLM_VENV/bin/python" -m pip install -q --no-deps --no-build-isolation -e "$REPO"
  if ! "$VLLM_VENV/bin/python" -c "import torch, torchaudio" 2>/dev/null; then
    "$VLLM_VENV/bin/python" -m pip install -q --no-deps "torch==$TORCH_VERSION" \
      "torchvision==$TORCHVISION_VERSION" "torchaudio==$TORCHAUDIO_VERSION" \
      --index-url https://download.pytorch.org/whl/cu128
  fi
fi

# ---------------------------------------------------------------- 4. Redis (AOF on /workspace)
if ! redis-cli -p 6379 ping >/dev/null 2>&1; then
  # requirepass comes from the store's include file, not the command line
  redis-server "$CDI_SECRETS_DIR/redis.conf" --port 6379 --bind 127.0.0.1 --dir "$WS/redis" \
    --appendonly yes --daemonize yes --logfile "$WS/logs/redis.log"
fi

# ---------------------------------------------------------------- 5. MinIO, built from source
# MinIO no longer publishes binaries (dl.min.io returns 410; releases carry no assets), so the
# pinned release tag is built from the official repository with the official Go toolchain.
# Pinned end to end (backlog E1-S4): the toolchain archive is checksum-verified, a toolchain of any
# other version is replaced, the tag must resolve to MINIO_COMMIT, Go may not switch to another
# toolchain, and module downloads are held to MinIO's own go.sum.
if [ "$(head -n1 "$WS/tools/go/VERSION" 2>/dev/null)" != "go$GO_VERSION" ]; then
  curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz"
  echo "$GO_SHA256  /tmp/go.tgz" | sha256sum -c -
  rm -rf "$WS/tools/go"
  tar -C "$WS/tools" -xzf /tmp/go.tgz && rm /tmp/go.tgz
fi
if [ ! -x "$MINIO_BIN" ]; then
  rm -rf /tmp/minio-src
  git clone --depth 1 --branch "$MINIO_TAG" https://github.com/minio/minio.git /tmp/minio-src
  HEAD_COMMIT=$(git -C /tmp/minio-src rev-parse HEAD)
  if [ "$HEAD_COMMIT" != "$MINIO_COMMIT" ]; then
    echo "DRIFT: minio tag $MINIO_TAG is commit $HEAD_COMMIT, pinned $MINIO_COMMIT"; exit 1
  fi
  ( cd /tmp/minio-src && GOTOOLCHAIN=local GOFLAGS="-trimpath -mod=readonly" \
      "$WS/tools/go/bin/go" build -o "$MINIO_BIN.partial" . )
  mv "$MINIO_BIN.partial" "$MINIO_BIN"
  rm -rf /tmp/minio-src
fi
if [ "${CDI_MINIO_MODE:-single}" != "single" ]; then
  # E10-S6 (docs/ha.md): "erasure" = one process, CDI_MINIO_DRIVES drives on this host (a lost drive);
  # "cluster" = this host's process of a multi-host set (a lost node; CDI_S3_ENDPOINT_URL lists
  # every host). Moving the single-drive store's objects in: python -m cdi_adapter.ha minio migrate
  ( cd "$REPO" && CDI_MINIO_BIN=$MINIO_BIN "$REPO/.venv/bin/python" -m cdi_adapter.ha minio start )
elif ! curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
  MINIO_ROOT_USER=$(secret get cdi_s3_access_key) MINIO_ROOT_PASSWORD=$(secret get cdi_s3_secret_key) \
    setsid nohup "$MINIO_BIN" server "$WS/minio-data" --address 127.0.0.1:9000 \
      --console-address 127.0.0.1:9001 >"$WS/logs/minio.log" 2>&1 </dev/null &
  for _ in $(seq 60); do curl -fsS http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1 && break; sleep 1; done
fi
"$REPO/.venv/bin/python" -c "from cdi_adapter.storage import get_store; get_store().ensure_bucket()"

# ---------------------------------------------------------------- 5b. HL7 validator + pinned ABDM IG
# backlog E6-S1: JRE from the apt pins; validator jar checksum-verified; the IG tarball and every
# FHIR package the validator uses installed from infra/fhir/packages.lock and proved by digest.
# Validation itself then always runs offline (validate_abdm.hapi_command, §12).
FHIR_HOME=${CDI_FHIR_PACKAGE_HOME:-$WS/tools/fhir-home}
FHIR_JAR=${CDI_FHIR_VALIDATOR_JAR:-$WS/tools/validator_cli.jar}
if [ "${CDI_IG_PACKAGE:-}" != "$FHIR_IG" ]; then
  echo "DRIFT: CDI_IG_PACKAGE=${CDI_IG_PACKAGE:-unset} but the pinned IG is $FHIR_IG (docs/ig-upgrade.md)"
  exit 1
fi
if [ "$(sha256sum "$FHIR_JAR" 2>/dev/null | cut -d' ' -f1)" != "$FHIR_VALIDATOR_SHA256" ]; then
  curl -fsSL -o "$FHIR_JAR.partial" \
    "https://github.com/hapifhir/org.hl7.fhir.core/releases/download/$FHIR_VALIDATOR_VERSION/validator_cli.jar"
  echo "$FHIR_VALIDATOR_SHA256  $FHIR_JAR.partial" | sha256sum -c -
  mv "$FHIR_JAR.partial" "$FHIR_JAR"
fi
"$REPO/.venv/bin/python" "$REPO/scripts/fhir_ig.py" seed --home "$FHIR_HOME"

# ---------------------------------------------------------------- 6. cron: DB snapshot every 15 min
service cron start >/dev/null 2>&1 || cron || true
( crontab -l 2>/dev/null | grep -v -e 'infra/runpod/snapshot.sh' -e 'infra/runpod/golden_nightly.sh' \
    -e 'cdi_adapter.dr drill' -e 'cdi_adapter.training export'
  echo "*/15 * * * * bash $REPO/infra/runpod/snapshot.sh >>$WS/logs/snapshot.log 2>&1"
  # E1-S6: nightly golden path on the real model (isolated DB + bucket; drift -> golden_alerts.log)
  echo "30 2 * * * bash $REPO/infra/runpod/golden_nightly.sh >>$WS/logs/golden.log 2>&1"
  # E4-S1: nightly export of the reviewers' decisions into the calibration training table
  echo "45 2 * * * cd $REPO && $REPO/.venv/bin/python -m cdi_adapter.training export >>$WS/logs/calibration_export.log 2>&1"
  # E10-S11: weekly restore drill from the backups alone (docs/dr.md); its report feeds the alerts
  echo "30 3 * * 0 cd $REPO && CDI_MINIO_BIN=$MINIO_BIN $REPO/.venv/bin/python -m cdi_adapter.dr drill >>$WS/logs/dr.log 2>&1" ) | crontab -

# ---------------------------------------------------------------- 7. version manifest + drift check
# Records this boot's versions in $WS/logs/manifests and checks every pin (backlog E1-S4). Drift
# stops the boot here, so start_all.sh never starts the web app on unpinned versions.
# CDI_ALLOW_DRIFT=1 lets an operator boot anyway (logged); nothing sets it by default.
DRIFT=0
"$REPO/.venv/bin/python" -m cdi_adapter.podmanifest write --dir "$WS/logs/manifests" \
     --versions "$HERE/versions.env" --lock "$LOCK" --ws "$WS" || DRIFT=1
if [ "${CDI_MLSERVE_BACKEND:-stub}" = "vllm" ]; then  # the gateway venv, against its own lock
  "$VLLM_VENV/bin/python" -m cdi_adapter.podmanifest write --dir "$WS/logs/manifests-vllm" \
       --versions "$HERE/versions.env" --lock "$HERE/requirements-vllm.lock" --ws "$WS" || DRIFT=1
fi
if [ "$DRIFT" = 1 ]; then
  if [ "${CDI_ALLOW_DRIFT:-0}" = 1 ]; then
    echo "WARNING: version drift (DRIFT lines above); continuing because CDI_ALLOW_DRIFT=1"
  else
    echo "bootstrap stopped: version drift from versions.env / requirements.lock (DRIFT lines above)"
    exit 1
  fi
fi

echo "=== bootstrap done $(date -Is) ==="
