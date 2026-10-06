#!/usr/bin/env bash
# A standalone test URL for a verification host (owner, 2026-09-25): a Cloudflare quick tunnel
# (https://<random>.trycloudflare.com, no account) to the website balancer, so the site can be
# opened, and handed to other testers, without the Colab browser window or its proxy URL.
#
#   tester --https--> Cloudflare --tunnel, dialled out by this host--> 127.0.0.1:$CDI_WEBAPP_PORT
#
# TEST HOSTS ONLY, synthetic / de-identified data: traffic is decrypted at Cloudflare, which is why
# a production pod is reached only through the E1-S9 ingress (infra/runpod/start_tunnel.sh). The
# website's own sign-in still guards every page. A quick tunnel's URL is new on every start.
#
#   bash infra/colab/start_test_tunnel.sh           # start, or print the running tunnel's URL
#   bash infra/colab/start_test_tunnel.sh status
#   bash infra/colab/start_test_tunnel.sh stop
#
# cloudflared is the pinned release (infra/runpod/versions.env), checksum-verified.
set -euo pipefail
WS=${WS:-/workspace}
REPO=${REPO:-/content/cdi}
set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
. "$REPO/infra/runpod/versions.env"
PORT=${CDI_WEBAPP_PORT:-8888}
BIN=$WS/tools/cloudflared-$CLOUDFLARED_VERSION/cloudflared
RUN=$WS/run
PID=$RUN/test-tunnel.pid
URLF=$RUN/test-tunnel.url
LOG=$WS/logs/test-tunnel.log
mkdir -p "$RUN" "$WS/logs" "$(dirname "$BIN")"

alive() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

case "${1:-start}" in
  status)
    if alive && [ -s "$URLF" ]; then echo "test tunnel up: $(cat "$URLF")"; else echo "test tunnel DOWN"; fi
    exit 0 ;;
  stop)
    if alive; then kill "$(cat "$PID")"; fi
    rm -f "$PID" "$URLF"
    echo "test tunnel stopped"
    exit 0 ;;
  start) ;;
  *) echo "usage: $0 [start|status|stop]"; exit 2 ;;
esac

if alive && [ -s "$URLF" ]; then
  echo "test tunnel already up: $(cat "$URLF")"
  exit 0
fi
curl -fsS -m 5 "http://127.0.0.1:$PORT/healthz" >/dev/null \
  || { echo "the website is not up on :$PORT; start it first (infra/runpod/start_apptier.sh)"; exit 1; }

if [ ! -x "$BIN" ]; then
  curl -fsSL -o "$BIN.partial" \
    "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
  echo "$CLOUDFLARED_SHA256  $BIN.partial" | sha256sum -c --quiet - \
    || { rm -f "$BIN.partial"; echo "sha256 mismatch for cloudflared $CLOUDFLARED_VERSION"; exit 1; }
  chmod 755 "$BIN.partial"
  mv "$BIN.partial" "$BIN"
fi
"$BIN" --version | grep -q "version $CLOUDFLARED_VERSION " \
  || { echo "cloudflared is not $CLOUDFLARED_VERSION"; exit 1; }

# its output goes to the log only: a %%bash cell or pipe reading this script is never held open
: >"$LOG"
setsid nohup "$BIN" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >>"$LOG" 2>&1 </dev/null &
echo $! >"$PID"
url=
for _ in $(seq 60); do
  url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$url" ] && break
  alive || { echo "cloudflared exited; see $LOG"; tail -5 "$LOG"; exit 1; }
  sleep 1
done
[ -n "$url" ] || { echo "no tunnel URL after 60 s; see $LOG"; exit 1; }
host=${url#https://}

# the sign-in and every form post are same-origin checked (E11-S1): allow the tunnel's host. An
# older tunnel's host is dropped; other hosts (the Colab proxy) are kept.
old=$(grep -E '^CDI_PUBLIC_ORIGINS=' "$REPO/.env" | tail -1 | cut -d= -f2- || true)
keep=$(printf '%s\n' "${old//,/$'\n'}" | grep -v -e 'trycloudflare\.com$' -e '^$' | paste -sd, - || true)
{ grep -v '^CDI_PUBLIC_ORIGINS=' "$REPO/.env" || true; echo "CDI_PUBLIC_ORIGINS=${keep:+$keep,}$host"; } \
  >"$REPO/.env.tunnel.tmp"
cat "$REPO/.env.tunnel.tmp" >"$REPO/.env"  # in place: .env keeps its owner and mode
rm -f "$REPO/.env.tunnel.tmp"

# the web instances read CDI_PUBLIC_ORIGINS at start: restart them (start_apptier restarts what is
# down and leaves the workers and the balancer as they are)
for f in "$RUN"/webapp-*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; done
sleep 3
REPO=$REPO WS=$WS bash "$REPO/infra/runpod/start_apptier.sh" >"$WS/logs/test-tunnel-apptier.log" 2>&1 \
  || { echo "website restart failed; see $WS/logs/test-tunnel-apptier.log"; exit 1; }

# a new quick-tunnel name takes a few seconds to resolve
for _ in $(seq 30); do
  curl -fsS -m 5 "$url/healthz" >/dev/null 2>&1 && break
  sleep 2
done
echo "$url" >"$URLF"
if curl -fsS -m 5 "$url/healthz" >/dev/null 2>&1; then
  echo "TEST URL: $url"
else
  echo "TEST URL: $url (not answering yet; DNS may still be propagating, try again in a minute)"
fi
