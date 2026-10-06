#!/usr/bin/env bash
# Deploy this working tree to a vast.ai GPU instance and bring the whole stack up (docs/vast-ai.md).
# Run from the repo root on the dev box (Git Bash), AFTER CI is green:
#
#   bash scripts/dev/vast_deploy.sh <ip> <ssh port>                      # deploy / update
#   bash scripts/dev/vast_deploy.sh <ip> <ssh port> --restore <backup.tar.gz>   # + restore a snapshot
#
# The ip and port are on the instance's card in the vast.ai console ("SSH connection"); the key is
# ~/.ssh/rx_gpu (add its .pub to the instance as an SSH key when renting). What it copies:
# the source bundle, the medicine catalogue (data/catalogue/tata1mg/, git-ignored, dev/test only)
# and, with --restore, a snapshot taken by scripts/dev/vast_backup.sh.
set -euo pipefail
HOST=${1:?usage: vast_deploy.sh <ip> <ssh port> [--restore <backup.tar.gz>]}
PORT=${2:?usage: vast_deploy.sh <ip> <ssh port> [--restore <backup.tar.gz>]}
RESTORE=
if [ "${3:-}" = "--restore" ]; then RESTORE=${4:?--restore needs a backup file}; fi
KEY=${VAST_KEY:-$HOME/.ssh/rx_gpu}
SSH=(ssh -i "$KEY" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$HOST")
SCP=(scp -i "$KEY" -P "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
PY=${PY:-.venv/Scripts/python.exe}
[ -x "$PY" ] || PY=.venv/bin/python
CATALOGUE=data/catalogue/tata1mg/indian_medicine_data.csv
BUNDLE=$(mktemp -d)/cdi-src.tar.gz

echo "== bundle"
"$PY" scripts/dev/colab_bundle.py --out "$BUNDLE" | head -1
echo "== copy to $HOST:$PORT"
"${SSH[@]}" "mkdir -p /workspace" 2>/dev/null
"${SCP[@]}" "$BUNDLE" "root@$HOST:/workspace/cdi-src.tar.gz"
if [ -f "$CATALOGUE" ]; then
  "${SCP[@]}" "$CATALOGUE" "root@$HOST:/workspace/catalogue.csv"
else
  echo "   (no $CATALOGUE here: the catalogue and the medication agent stay off on the host)"
fi
if [ -n "$RESTORE" ]; then "${SCP[@]}" "$RESTORE" "root@$HOST:/workspace/vast-backup.tar.gz"; fi
echo "== setup on the host (a fresh instance takes ~20-30 min: apt, venv, MinIO build, 7B download)"
"${SSH[@]}" "mkdir -p /tmp/vs && tar -xzf /workspace/cdi-src.tar.gz -C /tmp/vs infra/vastai/setup_vast.sh \
  && bash /tmp/vs/infra/vastai/setup_vast.sh" 2>&1 | grep -v -e 'Welcome to vast' -e 'Have fun' -e 'AI agents'
echo "== local access (keep this running):  ssh -i $KEY -p $PORT -N -L 18888:127.0.0.1:8888 root@$HOST"
echo "   then open http://localhost:18888"
