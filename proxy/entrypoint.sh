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

# If xray dies on its own (bad node, crash), let Docker restart the container.
while kill -0 "$xray_pid" 2>/dev/null; do
    sleep "$INTERVAL" &
    wait "$!"
    if python3 /app/build_config.py "${CONFIG}.new" 2>/dev/null && \
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
