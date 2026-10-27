#!/bin/sh
# Start nginx (the only public port) and uvicorn (unix sockets) with a watchdog.
# If /api/health fails repeatedly, exit non-zero so Docker
# `restart: unless-stopped` brings the app back (HEALTHCHECK alone doesn't).
set -eu

WEB_PORT="${WEB_PORT:-8790}"
case "$WEB_PORT" in
  ''|*[!0-9]*) echo "WEB_PORT must be a number, got: $WEB_PORT" >&2; exit 1 ;;
esac
mkdir -p "${CONFIG_DIR:-/config}"

# Sockets live inside the container, readable by nginx's workers (www-data) only.
SOCK_DIR=/run/ugreen-drive
mkdir -p "$SOCK_DIR"
chgrp www-data "$SOCK_DIR"
chmod 2770 "$SOCK_DIR"
rm -f "${SOCK_DIR:?}"/*.sock
umask 0007

sed "s/__WEB_PORT__/$WEB_PORT/" /etc/nginx/ugreen-drive.conf.template > /etc/nginx/conf.d/ugreen-drive.conf
nginx -t -q
nginx

INTERVAL="${IFD_WATCHDOG_INTERVAL_S:-15}"
TIMEOUT="${IFD_WATCHDOG_CURL_S:-5}"
FAILS_NEEDED="${IFD_WATCHDOG_FAILS:-3}"
START_GRACE="${IFD_WATCHDOG_START_S:-20}"

# Only nginx can reach the sockets, and it sets X-Forwarded-For/-Proto itself,
# so the app trusts those headers from any socket peer.
uvicorn main:app \
  --uds "$SOCK_DIR/app.sock" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --timeout-keep-alive 120 \
  --limit-concurrency 200 &
UV_PID=$!

# Upload workers: extra processes for chunk bodies (nginx upstream ugd_upload),
# so uploads aren't capped by one Python process's core. The count matches
# docker/nginx.conf.template.
UPLOAD_WORKERS=3
UPLOAD_PIDS=""

start_upload_worker() {
  IFD_UPLOAD_WORKER=1 uvicorn main:app \
    --uds "$SOCK_DIR/upload-$1.sock" \
    --proxy-headers \
    --forwarded-allow-ips '*' \
    --timeout-keep-alive 120 \
    --limit-concurrency 64 &
}

i=0
while [ "$i" -lt "$UPLOAD_WORKERS" ]; do
  start_upload_worker "$i"
  UPLOAD_PIDS="$UPLOAD_PIDS $i:$!"
  i=$((i + 1))
done

# Restart any upload worker that died (nginx falls back to the main app meanwhile).
revive_upload_workers() {
  alive=""
  for entry in $UPLOAD_PIDS; do
    n="${entry%%:*}"
    pid="${entry#*:}"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "watchdog: upload worker $n exited — restarting" >&2
      rm -f "$SOCK_DIR/upload-$n.sock"
      start_upload_worker "$n"
      pid=$!
    fi
    alive="$alive $n:$pid"
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
    while kill -0 "$UV_PID" 2>/dev/null && [ "$i" -lt 8 ]; do
      sleep 1
      i=$((i + 1))
    done
    kill -KILL "$UV_PID" 2>/dev/null || true
  fi
}
on_signal() {
  cleanup
  exit 0
}
trap on_signal INT TERM

# Sleep in the background and wait, so `docker stop` (TERM) runs the trap at once.
pause() {
  sleep "$1" &
  wait $! || true
}

health_ok() {
  curl -fsS --max-time "$TIMEOUT" --unix-socket "$SOCK_DIR/app.sock" http://localhost/api/health >/dev/null 2>&1
}

# Let uvicorn bind before probing.
pause "$START_GRACE"

fails=0
while kill -0 "$UV_PID" 2>/dev/null; do
  if health_ok; then
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
  pause "$INTERVAL"
done

# uvicorn exited on its own
wait "$UV_PID"
exit $?
