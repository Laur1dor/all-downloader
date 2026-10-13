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
NETWORK_SUBNET="${DEPLOY_NETWORK_SUBNET:-172.30.53.0/24}"
DEPLOYED_COMMIT="$LIVE/data/deployed_commit"
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
if [ -f "$DEPLOYED_COMMIT" ] && [ "$(cat "$DEPLOYED_COMMIT")" = "$remote_head" ]; then
    exit 0
fi

log "deploying ${local_head:0:8} -> ${remote_head:0:8}"
if [ "$local_head" != "$remote_head" ]; then
    git merge --ff-only --quiet origin/main
fi

# A stale compose file once stopped both bot and PostgreSQL, then tried to
# remove their shared network while other stacks still used it. Check the
# candidate before copying it to LIVE or changing any running container.
candidate="$REPO/compose.yml"
if ! grep -q '^  dns-pool:' "$candidate" \
        || ! grep -q '^  dns-warp:' "$candidate" \
        || ! grep -q '^networks:' "$candidate" \
        || ! grep -Fq "$NETWORK_SUBNET" "$candidate"; then
    log "refusing deploy: candidate compose is missing current DNS/network topology"
    notify "⚠️ Деплой отложен: compose не содержит действующую DNS/сетевую схему. Бот не остановлен."
    exit 1
fi
actual_subnet="$(docker network inspect "$NETWORK" \
    --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null || true)"
if [ "$actual_subnet" != "$NETWORK_SUBNET" ]; then
    log "refusing deploy: live network subnet differs from candidate"
    notify "⚠️ Деплой отложен: состав Docker сети требует ручной проверки. Бот не остановлен."
    exit 1
fi
compose_candidate=(docker compose --project-directory "$LIVE" -f "$candidate" \
    --env-file "$LIVE/.env")
if ! "${compose_candidate[@]}" config --quiet; then
    log "refusing deploy: candidate compose is invalid"
    notify "⚠️ Деплой отложен: compose не прошёл проверку. Бот не остановлен."
    exit 1
fi
if ! plan="$("${compose_candidate[@]}" --dry-run up -d --no-deps --no-build bot 2>&1)"; then
    log "refusing deploy: compose dry-run failed: $plan"
    notify "⚠️ Деплой отложен: Docker dry-run не прошёл. Бот не остановлен."
    exit 1
fi
if grep -Eq 'Network .* (Removing|Creating)|postgres-tiktok (Stopping|Recreate|Removing)' \
        <<< "$plan"; then
    log "refusing deploy: compose would modify the shared network or PostgreSQL"
    notify "⚠️ Деплой отложен: Docker собирался изменить общую сеть или БД. Бот не остановлен."
    exit 1
fi

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

# Keep the image actually running, which can differ from :latest after a
# previous failed deploy left a freshly built but unstarted image behind.
running_image="$(docker inspect tiktok-bot --format '{{.Image}}' 2>/dev/null || true)"
if [ -n "$running_image" ]; then
    docker tag "$running_image" "$IMAGE:previous"
fi

if ! docker compose build bot; then
    # A source-only update can reuse the verified runtime when package mirrors
    # are unreachable. Never do this across a dependency or Dockerfile change.
    if [ -n "$running_image" ] && [ -f "$DEPLOYED_COMMIT" ] \
            && git -C "$REPO" diff --quiet "$(cat "$DEPLOYED_COMMIT")" "$remote_head" \
                -- Dockerfile requirements.txt .dockerignore; then
        log "package build unavailable; reusing verified unchanged runtime"
        if ! docker build --pull=false -t "$IMAGE:latest" -f - . <<EOF
FROM $IMAGE:previous
USER root
RUN rm -rf /app/bot
COPY --chown=appuser:appuser bot/ /app/bot/
USER appuser
EOF
        then
            log "runtime overlay build failed — nothing restarted"
            exit 1
        fi
    else
        log "build failed — nothing restarted"
        notify "⚠️ Деплой остановлен: образ не собрался. Бот работает на прежней версии."
        exit 1
    fi
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

rollback() {
    log "deploy failed — restoring the previously running image"
    if docker image inspect "$IMAGE:previous" >/dev/null 2>&1; then
        docker tag "$IMAGE:previous" "$IMAGE:latest"
        if docker compose up -d --no-deps --no-build bot; then
            notify "🔁 Откат: новая версия не поднялась, вернул предыдущую."
        else
            log "rollback failed; operator action is required"
            notify "🚨 Деплой и откат не прошли; нужна ручная проверка бота."
        fi
    else
        notify "🚨 Новая версия не поднялась, предыдущий образ не найден."
    fi
}

if ! docker compose up -d --no-deps --no-build bot; then
    rollback
    exit 1
fi

healthy=0
for _ in $(seq 1 "$HEALTH_TRIES"); do
    sleep 3
    if curl -fsS --max-time 5 -o /dev/null "$HEALTH"; then
        healthy=1
        break
    fi
done

if [ "$healthy" -ne 1 ]; then
    rollback
    exit 1
fi

# git HEAD moved before the rollout; only this marker means the new image
# really passed health. A failed run is therefore retried by the timer.
marker_tmp="$(mktemp "$LIVE/data/deployed_commit.XXXXXX")"
printf '%s\n' "$remote_head" > "$marker_tmp"
mv -f "$marker_tmp" "$DEPLOYED_COMMIT"

# Keep unused BuildKit cache from accumulating on the small LXC disk.
# This never touches images, volumes or containers, and a pruning failure must
# not turn a healthy deployment into a failed one.
if prune_summary="$(timeout 90s docker builder prune \
        --reserved-space 2gb --force 2>&1 | tail -n 1)"; then
    log "build cache cleanup: $prune_summary"
else
    log "WARN: build cache cleanup failed: $prune_summary"
fi

# Only untagged images unused by any container are eligible. The named
# :previous rollback image and active images remain available.
if image_cleanup="$(timeout 60s docker image prune --force 2>&1 | tail -n 1)"; then
    log "untagged image cleanup: $image_cleanup"
else
    log "WARN: untagged image cleanup failed: $image_cleanup"
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
