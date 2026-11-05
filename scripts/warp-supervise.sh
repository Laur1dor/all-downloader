#!/bin/bash
# The vendor entrypoint waits for gost, not warp-svc. Exit if the tunnel
# daemon dies so Docker replaces the entire namespace instead of serving direct.
set -eu
"${1:-/entrypoint.sh}" &
entry=$!
trap 'kill -TERM "$entry" 2>/dev/null || true; exit 143' TERM INT
sleep "${WARP_WATCH_GRACE:-30}" &
wait "$!"
while kill -0 "$entry" 2>/dev/null; do
    if ! ps -eo stat,comm | awk '$2 == "warp-svc" && $1 !~ /^Z/ { live=1 } END { exit !live }'; then
        echo 'WARP daemon exited; restarting the proxy container' >&2
        kill -TERM "$entry" 2>/dev/null || true
        exit 1
    fi
    sleep "${WARP_WATCH_INTERVAL:-5}" &
    wait "$!"
done
wait "$entry"
