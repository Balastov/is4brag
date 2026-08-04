#!/bin/bash
# setup_cron.sh — ежедневная синхронизация Confluence → чанки (без индексации).
# Индекс: отдельно (ri_loop_host / sync без --skip-index / index_section.py).
# Запустить на хосте: bash "/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro/setup_cron.sh"

set -euo pipefail

CRON_LINE='0 21 * * * cd /home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU\ Metro && .venv/bin/python sync_confluence.py --skip-index >> sync.log 2>&1'

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# Убираем старые строки sync_confluence (с --skip-index или без) и ставим актуальную
crontab -l 2>/dev/null | grep -vF "sync_confluence.py" >"$TMP" || true
echo "$CRON_LINE" >>"$TMP"
crontab "$TMP"

echo "✅ Cron-задача обновлена (чанки only, --skip-index):"
crontab -l | grep "sync_confluence"
echo ""
echo "Индексацию запускайте отдельно, например:"
echo "  cd /home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU\\ Metro"
echo "  .venv/bin/python sync_confluence.py   # подхватит reindex_pending.json"
echo "  # или: nohup ./ri_loop_host.sh \"Стадии проекта\" &"
