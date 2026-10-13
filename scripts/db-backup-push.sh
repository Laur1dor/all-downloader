#!/bin/sh
# Daily backup of everything this machine could not be rebuilt without: the
# database, and the state files that live only on the server.
#
# Each run writes backups/db-YYYY-MM-DD.sql.gz so history shows what was saved
# when. Meant to run on the server from a clone of this repo (see PRIVATE-GUIDE).
#
# It also mirrors the live state files into the repo. Source already flows the
# other way - the deploy pulls it from here - but state never came back, because
# the deploy is deliberately careful not to touch it. So .env, the routing table
# and the cookie jar sat in the repo months out of date while reading as
# authoritative, which is the opposite of what a restore-from-nothing copy is
# for. The two directions cannot feed each other: the deploy writes only source,
# this writes only state.
#
# Push goes direct over SSH (ssh.github.com:443). If GitHub is unreachable from
# the server's IP (e.g. RU IP blocked), it transparently retries the push
# through the VLESS node's SOCKS proxy (the xray container).
set -eu

REPO_DIR="${BACKUP_REPO_DIR:-/root/all-downloader-private}"
ENV_FILE="${ENV_FILE:-/root/tiktok-bot/.env}"
PG_CONTAINER="${PG_CONTAINER:-postgres-tiktok}"
XRAY_CONTAINER="${XRAY_CONTAINER:-xray}"

cd "$REPO_DIR"
set -a; . "$ENV_FILE"; set +a

mkdir -p backups
STAMP="$(date +%Y-%m-%d)"
OUT="backups/db-${STAMP}.sql.gz"

docker exec "$PG_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$OUT"

git pull --quiet --no-edit || true
git add "$OUT"

# The state the server holds and a rebuild cannot recreate. Copied only if the
# live file exists and is not empty: a half-written jar or a truncated .env
# would otherwise overwrite the good copy with a worse one.
LIVE_DIR="${LIVE_DIR:-/root/tiktok-bot}"
for rel in .env data/routing.toml data/cookies.txt; do
    src="$LIVE_DIR/$rel"
    [ -s "$src" ] || continue
    mkdir -p "$(dirname "$rel")"
    if ! cmp -s "$src" "$rel"; then
        cp -a "$src" "$rel"
        # -f because data/ is ignored wholesale while these two files inside it
        # are tracked on purpose: they are state, and this is the private repo.
        git add -f "$rel"
        echo "mirrored $rel"
    fi
done
if git diff --cached --quiet; then
    echo "Nothing changed since the last backup ($STAMP)."
    exit 0
fi
git commit -q -m "Backup ${STAMP} ($(date +%H:%M))"

# 1) try a direct push.
if git push --quiet 2>/tmp/db-backup-push.err; then
    echo "Backed up ${OUT} and pushed (direct)."
    exit 0
fi

# 2) direct failed — route the push through the VLESS node's SOCKS proxy.
echo "Direct push failed, retrying through the VLESS proxy..." >&2
XIP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$XRAY_CONTAINER" 2>/dev/null)"
if [ -z "$XIP" ]; then
    echo "ERROR: xray container not found; cannot reach the proxy." >&2
    cat /tmp/db-backup-push.err >&2
    exit 1
fi
GIT_SSH_COMMAND="ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -i $HOME/.ssh/adl_deploy -o ProxyCommand='nc -X 5 -x ${XIP}:2080 %h %p'" \
    git push --quiet "git@github-adl:Laur1dor/all-downloader-private.git" HEAD:main
echo "Backed up ${OUT} and pushed (via VLESS)."
