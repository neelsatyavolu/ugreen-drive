#!/bin/sh
# Start nginx (the public port) and uvicorn (loopback only) with a watchdog.
# If /api/health fails repeatedly, exit non-zero so Docker
# `restart: unless-stopped` brings the app back (HEALTHCHECK alone doesn't).
set -eu

WEB_PORT="${WEB_PORT:-8790}"
case "$WEB_PORT" in
  ''|*[!0-9]*) echo "WEB_PORT must be a number, got: $WEB_PORT" >&2; exit 1 ;;
esac
mkdir -p "${CONFIG_DIR:-/config}"
sed "s/__WEB_PORT__/$WEB_PORT/" /etc/nginx/ugreen-drive.conf.template > /etc/nginx/conf.d/ugreen-drive.conf
nginx -t -q
nginx

HOST="${IFD_BIND_HOST:-127.0.0.1}"
PORT="${IFD_BIND_PORT:-8787}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
INTERVAL="${IFD_WATCHDOG_INTERVAL_S:-15}"
TIMEOUT="${IFD_WATCHDOG_CURL_S:-5}"
FAILS_NEEDED="${IFD_WATCHDOG_FAILS:-3}"
START_GRACE="${IFD_WATCHDOG_START_S:-20}"

uvicorn main:app \
  --host "$HOST" \
  --port "$PORT" \
  --proxy-headers \
  --timeout-keep-alive 120 \
  --limit-concurrency 200 &
UV_PID=$!

# Upload workers: extra processes for chunk bodies (nginx upstream ifd_upload),
# so uploads aren't capped by one Python process's core. Loopback only.
UPLOAD_WORKERS="${IFD_UPLOAD_WORKERS:-3}"
UPLOAD_PORT_BASE="${IFD_UPLOAD_PORT_BASE:-8793}"
UPLOAD_PIDS=""

start_upload_worker() {
  IFD_UPLOAD_WORKER=1 uvicorn main:app \
    --host 127.0.0.1 \
    --port "$1" \
    --proxy-headers \
    --timeout-keep-alive 120 \
    --limit-concurrency 64 &
}

i=0
while [ "$i" -lt "$UPLOAD_WORKERS" ]; do
  port=$((UPLOAD_PORT_BASE + i))
  start_upload_worker "$port"
  UPLOAD_PIDS="$UPLOAD_PIDS $port:$!"
  i=$((i + 1))
done

# Restart any upload worker that died (nginx falls back to the main app meanwhile).
revive_upload_workers() {
  alive=""
  for entry in $UPLOAD_PIDS; do
    port="${entry%%:*}"
    pid="${entry#*:}"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "watchdog: upload worker :$port exited — restarting" >&2
      start_upload_worker "$port"
      pid=$!
    fi
    alive="$alive $port:$pid"
  done
  UPLOAD_PIDS="$alive"
}

cleanup() {
  nginx -s quit 2>/dev/null || true
  for entry in $UPLOAD_PIDS; do
    kill -TERM "${entry#*:}" 2>/dev/null || true
  done
  if kill -0 "$UV_PID" 2>/dev/null; then
    kill -TERM "$UV_PID" 2>/dev/null || true
    # Brief grace, then force
    i=0
    while kill -0 "$UV_PID" 2>/dev/null && [ "$i" -lt 10 ]; do
      sleep 1
      i=$((i + 1))
    done
    kill -KILL "$UV_PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM

# Let uvicorn bind before probing.
sleep "$START_GRACE"

fails=0
while kill -0 "$UV_PID" 2>/dev/null; do
  if curl -fsS --max-time "$TIMEOUT" "$HEALTH_URL" >/dev/null 2>&1; then
    fails=0
  else
    fails=$((fails + 1))
    echo "watchdog: health failed ($fails/$FAILS_NEEDED)" >&2
    if [ "$fails" -ge "$FAILS_NEEDED" ]; then
      echo "watchdog: restarting — /api/health unresponsive" >&2
      cleanup
      exit 1
    fi
  fi
  revive_upload_workers
  if ! { [ -f /run/nginx.pid ] && kill -0 "$(cat /run/nginx.pid)" 2>/dev/null; }; then
    echo "watchdog: nginx exited — restarting" >&2
    nginx || true
  fi
  sleep "$INTERVAL"
done

# uvicorn exited on its own
wait "$UV_PID"
exit $?
