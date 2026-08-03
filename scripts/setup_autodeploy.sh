#!/usr/bin/env bash
# setup_autodeploy.sh — одноразовая установка автодеплоя на master@rag.
#
#   bash scripts/setup_autodeploy.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Balastov/is4brag.git}"
REPO_DIR="${REPO_DIR:-$HOME/is4brag}"
BRANCH="${AUTODEPLOY_BRANCH:-main}"
CRON_EVERY_MINUTES="${CRON_EVERY_MINUTES:-5}"

KISU_BASE="${KISU_BASE:-/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro}"
SKILLS_PUBLIC="${SKILLS_PUBLIC:-/home/master/deer-flow/skills/public}"

echo "==> Repo: $REPO_DIR  ($REPO_URL @ $BRANCH)"
echo "==> SKILLS_PUBLIC=$SKILLS_PUBLIC"

if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
else
    git -C "$REPO_DIR" fetch --prune origin
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
fi

chmod +x "$REPO_DIR/scripts/"*.sh

export KISU_BASE SKILLS_PUBLIC
bash "$REPO_DIR/scripts/deploy.sh"

PULL_SH="$REPO_DIR/scripts/autodeploy_pull.sh"
# SKILLS_PUBLIC в cron — на случай старых копий deploy.sh без дефолта
CRON_LINE="*/${CRON_EVERY_MINUTES} * * * * SKILLS_PUBLIC=${SKILLS_PUBLIC} ${PULL_SH}"

TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'autodeploy_pull.sh' >"$TMP" || true
echo "$CRON_LINE" >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

echo ""
echo "==> Autodeploy установлен"
echo "    Клон:     $REPO_DIR"
echo "    Cron:     $CRON_LINE"
echo "    Лог:      $REPO_DIR/deploy.log"
echo "    KISU:     $KISU_BASE"
echo "    Skills:   $SKILLS_PUBLIC"
echo ""
echo "Проверка:"
echo "  crontab -l | grep autodeploy"
echo "  grep E5_QUERY_PREFIX \"$SKILLS_PUBLIC/kisu-metro/scripts/kisu_metro_search.py\""
echo "  tail -f $REPO_DIR/deploy.log"
