#!/usr/bin/env bash
# DB snapshot (ARCHITECTURE §10.3): pg_dump -Fc -> /workspace/backup/cdi.dump, keep the last 10.
# cron runs this every 15 minutes; run it by hand before a planned pod stop.
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
# credentials come from the secrets store via libpq's password file (backlog E1-S5)
export PGPASSFILE=${PGPASSFILE:-${CDI_SECRETS_DIR:-/run/cdi-secrets}/pgpass}
URL=postgresql://cdi@127.0.0.1:5432/cdi
DIR=$WS/backup
mkdir -p "$DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TAKEN=$(date -u -d "${STAMP:0:8} ${STAMP:9:2}:${STAMP:11:2}:${STAMP:13:2}" +%Y-%m-%dT%H:%M:%SZ)
TMP="$DIR/.cdi.$STAMP.dump.partial"
PY="$REPO/.venv/bin/python"
# E10-S11: any failure is recorded, so the monitoring sees a stale backup (docs/dr.md)
trap '"$PY" -m cdi_adapter.dr backup --failed "snapshot.sh line $LINENO" --taken-at "$TAKEN" || true' ERR

pg_dump -Fc -d "$URL" -f "$TMP"
pg_restore --list "$TMP" >/dev/null           # a dump that cannot be listed is never promoted
mv "$TMP" "$DIR/cdi.$STAMP.dump"
cp "$DIR/cdi.$STAMP.dump" "$DIR/cdi.dump.new" && mv "$DIR/cdi.dump.new" "$DIR/cdi.dump"
ls -1t "$DIR"/cdi.*Z.dump 2>/dev/null | tail -n +11 | xargs -r rm -f
# the scans and pages next to it: new MinIO objects copied, sha256-checked, and the status written
( cd "$REPO" && CDI_BACKUP_DIR="$DIR" "$PY" -m cdi_adapter.dr backup --dump "cdi.$STAMP.dump" --taken-at "$TAKEN" )
echo "snapshot $STAMP ok ($(du -h "$DIR/cdi.dump" | cut -f1))"
