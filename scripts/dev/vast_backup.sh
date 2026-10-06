#!/usr/bin/env bash
# Take a snapshot on a vast.ai instance and download it (docs/vast-ai.md), e.g. before destroying
# the instance. The snapshot is infra/runpod/snapshot.sh: the database dump plus every document scan
# with its sha256 manifest. It lands in data/vast-backup/ (git-ignored: it holds the test data).
#
#   bash scripts/dev/vast_backup.sh <ip> <ssh port>
set -euo pipefail
HOST=${1:?usage: vast_backup.sh <ip> <ssh port>}
PORT=${2:?usage: vast_backup.sh <ip> <ssh port>}
KEY=${VAST_KEY:-$HOME/.ssh/rx_gpu}
SSH=(ssh -i "$KEY" -p "$PORT" -o BatchMode=yes "root@$HOST")
"${SSH[@]}" "cd /workspace/cdi && bash infra/runpod/snapshot.sh | tail -1 \
  && tar -czf /workspace/vast-backup.tar.gz -C /workspace backup \
  && sha256sum /workspace/vast-backup.tar.gz" 2>&1 | grep -v -e 'Welcome to vast' -e 'Have fun' -e 'AI agents'
mkdir -p data/vast-backup
OUT=data/vast-backup/vast-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz
scp -i "$KEY" -P "$PORT" -o BatchMode=yes "root@$HOST:/workspace/vast-backup.tar.gz" "$OUT"
sha256sum "$OUT"
echo "saved $OUT (compare the two sha256 lines above)"
