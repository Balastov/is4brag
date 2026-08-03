#!/bin/bash
# setup_cron.sh — добавить cron-задачу для ежедневной синхронизации Confluence → индексы
# Запустить на хосте: bash "/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro/setup_cron.sh"

CRON_LINE='0 21 * * * cd /home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU\ Metro && .venv/bin/python sync_confluence.py >> sync.log 2>&1'

# Проверяем, не добавлена ли уже задача
if crontab -l 2>/dev/null | grep -qF "sync_confluence.py"; then
    echo "✅ Cron-задача уже существует:"
    crontab -l | grep "sync_confluence"
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "✅ Cron-задача добавлена:"
    echo "$CRON_LINE"
fi
