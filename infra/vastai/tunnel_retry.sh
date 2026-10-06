#!/usr/bin/env bash
# Keep asking Cloudflare for the quick test tunnel until it is granted (docs/vast-ai.md): a vast.ai
# IP is shared, so trycloudflare.com often answers 429 (rate limit). Every 2 minutes, 30 tries.
WS=${WS:-/workspace}
REPO=${REPO:-$WS/cdi}
export WS REPO
for i in $(seq 1 30); do
  out=$(bash "$REPO/infra/colab/start_test_tunnel.sh" 2>&1 | tail -2)
  echo "$(date -u +%H:%M:%S) try $i: $out"
  echo "$out" | grep -q "TEST URL\|already up" && exit 0
  sleep 120
done
exit 1
