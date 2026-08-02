#!/usr/bin/env bash
# autodeploy_pull.sh — git fetch + deploy при появлении новых коммитов на origin/main.
# Вызывается cron'ом (см. setup_autodeploy.sh).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="${AUTODEPLOY_BRANCH:-main}"
REMOTE="${AUTODEPLOY_REMOTE:-origin}"
LOCK_FILE="${REPO_DIR}/.autodeploy.lock"
LOG_FILE="${REPO_DIR}/deploy.log"

cd "$REPO_DIR"

# Не даём параллельным cron-запускам наложиться
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] skip: already running" >>"$LOG_FILE"
    exit 0
fi

{
    echo "======== $(date '+%Y-%m-%d %H:%M:%S') ========"
    git fetch --prune "$REMOTE" "$BRANCH"

    LOCAL="$(git rev-parse HEAD)"
    REMOTE_REV="$(git rev-parse "$REMOTE/$BRANCH")"

    if [[ "$LOCAL" == "$REMOTE_REV" ]]; then
        echo "up-to-date $LOCAL"
        exit 0
    fi

    echo "update $LOCAL → $REMOTE_REV"
    git reset --hard "$REMOTE/$BRANCH"
    bash "$REPO_DIR/scripts/deploy.sh"
    echo "deployed $(git rev-parse --short HEAD)"
} >>"$LOG_FILE" 2>&1
