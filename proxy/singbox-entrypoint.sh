#!/bin/sh
# Run sing-box off the links the admin sent to /vpn, and pick up a replacement
# without anyone touching the machine: the bot rewrites data/vpn/singbox.txt and
# drops a reload marker, and this loop restarts sing-box on it.
#
# The bot signals through a file rather than the docker socket on purpose —
# handing the bot container the socket would hand it root on the host.
set -eu

CONFIG=/etc/sing-box/config.json
LINKS="${SINGBOX_LINKS:-/app/data/vpn/singbox.txt}"
RELOAD="${SINGBOX_RELOAD_FILE:-/app/data/vpn/reload_singbox}"
POLL="${SINGBOX_POLL:-15}"

start() {
    python3 /app/singbox_config.py "$CONFIG"
    sing-box run -c "$CONFIG" &
    sb_pid=$!
}

# With no links yet there is nothing to run, so wait for the first one instead
# of crash-looping and burning the restart budget.
while [ ! -s "$LINKS" ]; do
    echo "sing-box: waiting for $LINKS"
    sleep "$POLL"
done

start
echo "sing-box: started (pid $sb_pid)"

while kill -0 "$sb_pid" 2>/dev/null; do
    sleep "$POLL"
    if [ -f "$RELOAD" ]; then
        rm -f "$RELOAD"
        echo "sing-box: new config — restarting"
        kill "$sb_pid" 2>/dev/null || true
        wait "$sb_pid" 2>/dev/null || true
        start
    fi
done

wait "$sb_pid"
