#!/bin/sh
# Waits for the bot to ask for a login, performs it, and goes back to sleep.
#
# The bot signals through a file, the way the tunnels already do, rather than
# through the docker socket — handing the bot container the socket would hand it
# root on the host, and a login helper is not worth that.
set -eu

REQUEST="${IG_REQUEST_FILE:-/app/data/iglogin_request}"
POLL="${IG_POLL:-10}"

# The same browser answers two questions: sign in, and what is in this post.
# The resolver runs alongside rather than inside this loop, because a login can
# take minutes waiting on a mailed code and a post must not queue behind it.
python /app/scripts/igresolve.py &
RESOLVER=$!
echo "iglogin: resolver started (pid $RESOLVER)"

# And a third: on request, put this browser's screen in front of a person so
# they can answer a CAPTCHA on the account. Idle otherwise - nothing listens.
python /app/scripts/igremote.py &
echo "iglogin: remote screen helper started (pid $!)"

echo "iglogin: waiting for a request at $REQUEST"
while true; do
    if [ -f "$REQUEST" ] && [ -f /app/data/igremote.active ]; then
        # A person has the browser; the login waits for them to finish.
        sleep "$POLL"
        continue
    fi
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
