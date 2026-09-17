#!/bin/bash
# Pull-based deploy. A timer runs this on the box; nothing reaches in from
# outside, so there is no inbound SSH to open and no deploy key to leak into a
# CI provider. The machine decides when to go and fetch.
#
# Four gates, in order, because a deploy that cannot be undone is not a deploy:
#   1. nothing to do unless origin/main actually moved
#   2. the image has to build
#   3. the tests have to pass IN THAT IMAGE, before anything is restarted
#   4. the bot has to come back healthy, or the previous image is put back
#
# Source of truth is the private repo: the public one is a mirror without the
# operator's notes, and deploying from a mirror is how the two drift apart.
set -euo pipefail

REPO="${DEPLOY_REPO:-/root/deploy-src}"
LIVE="${DEPLOY_LIVE:-/root/tiktok-bot}"
IMAGE="${DEPLOY_IMAGE:-tiktok-bot-bot}"
HEALTH="${DEPLOY_HEALTH:-http://127.0.0.1:30080/health}"
HEALTH_TRIES="${DEPLOY_HEALTH_TRIES:-20}"
NETWORK="${DEPLOY_NETWORK:-tiktok-bot_default}"
API="${DEPLOY_API:-http://telegram-bot-api:8081}"

log() { echo "[$(date -Is)] $*"; }

# .env stays where it is and is never copied into the repo; the token is only
# borrowed here to say what happened.
#
# The message goes through the self-hosted Bot API on the compose network rather
# than to api.telegram.org: this host has no direct route to Telegram at all,
# which is the entire reason the tunnel exists. A throwaway container rather than
# docker exec into the bot, because the report worth sending most is the one
# about a bot that did not come back up.
notify() {
    local text="$1" token chat
    token="$(grep -oP '^BOT_TOKEN=\K.*' "$LIVE/.env" 2>/dev/null || true)"
    chat="$(grep -oP '^ADMIN_ID=\K.*' "$LIVE/.env" 2>/dev/null || true)"
    [ -n "$token" ] && [ -n "$chat" ] || return 0
    # The URL carries the bot token, so it goes in on stdin rather than on the
    # command line: anything on a command line is readable by every process on
    # the machine, through ps, for as long as the command runs.
    printf 'url = "%s/bot%s/sendMessage"\n' "$API" "$token" \
        | docker run --rm -i --network "$NETWORK" curlimages/curl:latest \
            -fsS --max-time 20 -o /dev/null \
            --data-urlencode "chat_id=${chat}" \
            --data-urlencode "text=${text}" \
            --config - >/dev/null 2>&1 || true
}

cd "$REPO"
git fetch --quiet origin main
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse origin/main)"
if [ "$local_head" = "$remote_head" ]; then
    exit 0
fi

log "deploying ${local_head:0:8} -> ${remote_head:0:8}"
git merge --ff-only --quiet origin/main

# Only the source. data/, .env and the backups live in $LIVE and must survive a
# deploy untouched — they are the things a rebuild cannot recreate.
for item in bot proxy tests scripts requirements.txt compose.yml \
            Dockerfile Dockerfile.xray Dockerfile.singbox \
            Dockerfile.awg Dockerfile.botapi botapi-entrypoint.sh; do
    [ -e "$REPO/$item" ] || continue
    rsync -a --delete-after "$REPO/$item" "$LIVE/" 2>/dev/null \
        || cp -r "$REPO/$item" "$LIVE/"
done

cd "$LIVE"

# Keep the image that is currently working, so there is something to go back to.
if docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
    docker tag "$IMAGE:latest" "$IMAGE:previous"
fi

if ! docker compose build bot; then
    log "build failed — nothing restarted"
    notify "⚠️ Деплой остановлен: образ не собрался. Бот работает на прежней версии."
    exit 1
fi

# In the image that is about to run, not in the one that is running now.
# Read-only: this mounts the live directory, .env and data/ included, into a
# container to run tests in. They have no business writing there, and a test that
# ever tried would be deleting the one thing a rebuild cannot recreate.
if ! docker run --rm -v "$LIVE:/src:ro" -w /src "$IMAGE:latest" \
        python -m tests.smoke_test; then
    log "tests failed — nothing restarted"
    notify "⚠️ Деплой остановлен: тесты не прошли. Бот работает на прежней версии."
    exit 1
fi

docker compose up -d bot

healthy=0
for _ in $(seq 1 "$HEALTH_TRIES"); do
    sleep 3
    if curl -fsS --max-time 5 -o /dev/null "$HEALTH"; then
        healthy=1
        break
    fi
done

if [ "$healthy" -ne 1 ]; then
    log "unhealthy after deploy — rolling back"
    if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
        docker tag "$IMAGE:previous" "$IMAGE:latest"
        docker compose up -d bot
        notify "🔁 Откат: новая версия не поднялась, вернул предыдущую."
    else
        notify "⚠️ Новая версия не поднялась, и откатываться не на что."
    fi
    exit 1
fi

# The deploy updates itself, but only after a run has succeeded, and only for
# the next run: bash reads a script as it executes, so rewriting the file that is
# currently running is a way to end up executing half of each version.
if ! cmp -s "$LIVE/scripts/selfdeploy.sh" /usr/local/bin/selfdeploy.sh; then
    install -m 755 "$LIVE/scripts/selfdeploy.sh" /usr/local/bin/selfdeploy.sh
    log "deploy script updated; it takes effect next run"
fi
units_changed=0
for unit in selfdeploy.service selfdeploy.timer; do
    [ -f "$LIVE/scripts/$unit" ] || continue
    if ! cmp -s "$LIVE/scripts/$unit" "/etc/systemd/system/$unit"; then
        install -m 644 "$LIVE/scripts/$unit" "/etc/systemd/system/$unit"
        units_changed=1
    fi
done
if [ "$units_changed" -eq 1 ]; then
    systemctl daemon-reload
    log "systemd units updated"
fi

subject="$(git -C "$REPO" log -1 --pretty=%s)"
log "deployed ${remote_head:0:8}"
notify "✅ Задеплоено ${remote_head:0:8}: ${subject}"
