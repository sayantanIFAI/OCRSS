#!/usr/bin/env bash
# Bring a verification host's code to exactly a verified local tree (owner, 2026-09-25: keep Colab
# synchronized with the verified local code; no hand-patching per test).
#
# Input: the bundle built locally by scripts/dev/colab_bundle.py after a green CI gate, uploaded
# to the host. This script
#   1. checks every file of the bundle against its MANIFEST.sha256 (a damaged upload changes nothing)
#   2. removes the files the previous bundle had and this one does not (only those: .env, .venv,
#      data and logs are never in a bundle), then applies the tree
#   3. checks the host tree against the manifest again
#   4. reinstalls the pinned packages if requirements.lock changed; migrates the database
#   5. restarts the web instances and job workers (and the model gateway if its code changed)
# and records the bundle id in $REPO/.bundle_id. A fresh VM runs the notebook's setup steps first.
#
#   tar -xzf /content/cdi-src.tar.gz -C /tmp infra/colab/sync.sh && bash /tmp/infra/colab/sync.sh
#   bash /content/cdi/infra/colab/sync.sh [/content/cdi-src.tar.gz]
set -euo pipefail
ARC=${1:-/content/cdi-src.tar.gz}
WS=${WS:-/workspace}
REPO=${REPO:-/content/cdi}
M=MANIFEST.sha256
[ -f "$ARC" ] || { echo "no bundle at $ARC (upload cdi-src.tar.gz to /content first)"; exit 1; }
[ -x "$REPO/.venv/bin/python" ] || { echo "$REPO has no .venv: run the notebook's setup steps first"; exit 1; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$ARC" -C "$STAGE"
[ -f "$STAGE/$M" ] || { echo "not a colab_bundle.py bundle (no $M); nothing changed"; exit 1; }
(cd "$STAGE" && sha256sum -c --quiet "$M") || { echo "bundle damaged (checksum mismatch); nothing changed"; exit 1; }
ID=$(sha256sum "$STAGE/$M" | cut -c1-12)
FILES=$(wc -l <"$STAGE/$M")
OLD=$REPO/$M
paths() { cut -c67- "$1" | sort; }  # "<64 hex>  <path>" -> path (paths may hold spaces)
# changed <prefix>: does any file under <prefix> differ from the previous bundle (no record = yes)
changed() {
  [ -f "$OLD" ] || return 0
  ! cmp -s <(grep -F "  $1" "$OLD" | sort || true) <(grep -F "  $1" "$STAGE/$M" | sort || true)
}
LOCK_CHANGED=0; changed infra/runpod/requirements.lock && LOCK_CHANGED=1
GATEWAY_CHANGED=0
for p in src/cdi_adapter/mlserve/ src/cdi_adapter/config.py infra/runpod/start_mlserve.sh; do
  changed "$p" && GATEWAY_CHANGED=1
done

if [ -f "$OLD" ]; then
  comm -23 <(paths "$OLD") <(paths "$STAGE/$M") | while IFS= read -r f; do
    rm -f "$REPO/$f" && echo "removed $f"
  done
fi
(cd "$STAGE" && tar -cf - .) | (cd "$REPO" && tar -xf -)
(cd "$REPO" && sha256sum -c --quiet "$M") || { echo "host tree does not match bundle $ID"; exit 1; }

set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
if [ "$LOCK_CHANGED" = 1 ]; then
  echo "requirements.lock changed: installing the pinned packages"
  "$REPO/.venv/bin/python" -m pip install -q --require-hashes --no-deps -r "$REPO/infra/runpod/requirements.lock"
  "$REPO/.venv/bin/python" -m pip install -q --no-deps --no-build-isolation -e "$REPO"
fi
(cd "$REPO" && .venv/bin/python -m alembic upgrade head 2>&1 | grep -v '^INFO  \[alembic.runtime.migration\] \(Context impl\|Will assume\)' || true)
HEAD=$(cd "$REPO" && .venv/bin/python -m alembic current 2>/dev/null | tail -1)

# web instances + job workers run the new code (start_apptier.sh starts what is down)
for f in "$WS"/run/webapp-*.pid "$WS"/run/worker-*.pid; do
  [ -f "$f" ] && { kill "$(cat "$f")" 2>/dev/null || true; }
done
sleep 5
REPO=$REPO WS=$WS bash "$REPO/infra/runpod/start_apptier.sh" >"$WS/logs/sync-apptier.log" 2>&1 \
  || { echo "website restart failed; see $WS/logs/sync-apptier.log"; exit 1; }
if [ "$GATEWAY_CHANGED" = 1 ] && pgrep -f cdi_adapter.mlserve >/dev/null; then
  echo "model gateway code changed: restarting it (the model reloads on the first request)"
  pkill -f cdi_adapter.mlserve || true
  sleep 3
  REPO=$REPO WS=$WS HF_HOME=${HF_HOME:-$WS/hf-cache} bash "$REPO/infra/runpod/start_mlserve.sh" \
    >"$WS/logs/sync-mlserve.log" 2>&1 || echo "gateway restart failed; see $WS/logs/sync-mlserve.log"
fi
echo "$ID" >"$REPO/.bundle_id"
echo "SYNCED: bundle $ID, $FILES files, database $HEAD"
REPO=$REPO WS=$WS bash "$REPO/infra/runpod/start_apptier.sh" status | head -"$(( ${CDI_WEBAPP_INSTANCES:-2} + ${CDI_WORKERS:-2} ))"
