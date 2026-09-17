#!/bin/sh
# Waits for the bot to ask for a login, performs it, and goes back to sleep.
#
# The bot signals through a file, the way the tunnels already do, rather than
# through the docker socket — handing the bot container the socket would hand it
# root on the host, and a login helper is not worth that.
set -eu

REQUEST="${IG_REQUEST_FILE:-/app/data/iglogin_request}"
POLL="${IG_POLL:-10}"

echo "iglogin: waiting for a request at $REQUEST"
while true; do
    if [ -f "$REQUEST" ]; then
        rm -f "$REQUEST"
        echo "iglogin: request received"
        # Never fatal: a failed login must not take the container down, or the
        # next request would find nothing listening.
        python /app/scripts/iglogin_browser.py || echo "iglogin: attempt failed"
        echo "iglogin: back to waiting"
    fi
    sleep "$POLL"
done
