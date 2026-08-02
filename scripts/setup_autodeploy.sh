#!/usr/bin/env bash
# setup_autodeploy.sh — одноразовая установка автодеплоя на master@rag.
#
#   bash scripts/setup_autodeploy.sh
#   REPO_DIR=/opt/is4brag bash scripts/setup_autodeploy.sh
#
# После установки: каждый push в main подтянется в течение ~5 минут (cron)
# и файлы скопируются в KISU Metro + skills/public.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Balastov/is4brag.git}"
REPO_DIR="${REPO_DIR:-$HOME/is4brag}"
BRANCH="${AUTODEPLOY_BRANCH:-main}"
CRON_EVERY_MINUTES="${CRON_EVERY_MINUTES:-5}"

KISU_BASE="${KISU_BASE:-/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro}"

echo "==> Repo: $REPO_DIR  ($REPO_URL @ $BRANCH)"

if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
else
    git -C "$REPO_DIR" fetch --prune origin
    git -C "$REPO_DIR" checkout "$BRANCH"
    git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
fi

chmod +x "$REPO_DIR/scripts/"*.sh

# Первый деплой сразу
export KISU_BASE
if [[ -n "${SKILLS_PUBLIC:-}" ]]; then
    export SKILLS_PUBLIC
fi
bash "$REPO_DIR/scripts/deploy.sh"

PULL_SH="$REPO_DIR/scripts/autodeploy_pull.sh"
CRON_LINE="*/${CRON_EVERY_MINUTES} * * * * $PULL_SH"

# Обновить/добавить cron-строку autodeploy
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
echo ""
echo "Проверка:"
echo "  crontab -l | grep autodeploy"
echo "  tail -f $REPO_DIR/deploy.log"
echo ""
echo "Ручной прогон:"
echo "  bash $PULL_SH"
