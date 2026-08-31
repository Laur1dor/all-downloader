#!/bin/sh
# Generate the xray config from the subscription/configs, run xray, and refresh
# the subscription periodically — restarting xray only when the config changed.
set -eu

CONFIG=/etc/xray/config.json
INTERVAL="${VLESS_UPDATE_INTERVAL:-21600}"  # default: refresh every 6 hours

/usr/local/bin/netshape.sh || true   # optional egress bandwidth cap

python3 /app/build_config.py "$CONFIG"

xray run -c "$CONFIG" &
xray_pid=$!

# Verify free nodes in the background: only ~2% of the subscription links get
# through the DPI, so the pool has to be filled from tested nodes rather than
# from the top of the list. The prober writes data/goida_live.txt; the refresh
# loop below picks it up and reloads xray when the set changed.
if [ -n "${GOIDA_SUBSCRIPTIONS:-}" ]; then
    python3 /app/probe_goida.py &
fi

# If xray dies on its own (bad node, crash), let Docker restart the container.
REBUILD_FILE="${XRAY_REBUILD_FILE:-/app/data/rebuild_xray}"
POLL=30
while kill -0 "$xray_pid" 2>/dev/null; do
    # Wait out the interval, but wake early when the prober says the pool
    # actually changed. Rebuilding on a timer alone forced a choice between
    # restarting xray far more often than the nodes change — every reload drops
    # the connections running through it — and leaving a dead pool in place for
    # the whole interval. Waiting on the event does neither.
    waited=0
    while [ "$waited" -lt "$INTERVAL" ]; do
        if [ -f "$REBUILD_FILE" ]; then
            rm -f "$REBUILD_FILE"
            break
        fi
        sleep "$POLL" &
        wait "$!"
        waited=$((waited + POLL))
        kill -0 "$xray_pid" 2>/dev/null || break
    done
    kill -0 "$xray_pid" 2>/dev/null || break
    # stderr is kept: its WARNs (dropped nodes, a pool that failed to build) are
    # the only trace of why a refresh produced a smaller pool than expected.
    if python3 /app/build_config.py "${CONFIG}.new" && \
       ! cmp -s "$CONFIG" "${CONFIG}.new"; then
        mv "${CONFIG}.new" "$CONFIG"
        echo "Subscription changed — reloading xray"
        kill "$xray_pid" 2>/dev/null || true
        wait "$xray_pid" 2>/dev/null || true
        xray run -c "$CONFIG" &
        xray_pid=$!
    else
        rm -f "${CONFIG}.new"
    fi
done

wait "$xray_pid"
