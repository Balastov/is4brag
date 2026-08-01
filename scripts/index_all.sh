#!/bin/bash
# ============================================================
# Запуск индексации ВСЕХ разделов KISU Metro
# ЗАПУСКАТЬ НА ХОСТЕ, НЕ ЧЕРЕЗ АГЕНТА:
#   cd /mnt/write/KISU Metro
#   python3 -m venv .venv && .venv/bin/pip install numpy sentence-transformers scikit-learn requests python-dotenv
#   nohup bash index_all.sh > index_all.log 2>&1 &
#   tail -f index_all.log
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

SECTIONS=(
    "Термины и сокращения"
    "Архитектура"
    "Управление проектом"
    "Стадии проекта"
)

START=$(date "+%Y-%m-%d %H:%M:%S")
echo "========================================"
echo "🚀 Индексация KISU Metro | Старт: $START"
echo "   Разделов: ${#SECTIONS[@]}"
echo "   Модель: intfloat/multilingual-e5-large"
echo "========================================"
echo ""

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ Не найден $VENV_PYTHON — создайте venv: python3 -m venv .venv"
    exit 1
fi

for sec in "${SECTIONS[@]}"; do
    echo ">>> $sec"
    "$VENV_PYTHON" "$SCRIPT_DIR/index_section.py" "$sec"
done

END=$(date "+%Y-%m-%d %H:%M:%S")
echo "========================================"
echo "✅ ВСЁ ГОТОВО | $START → $END"
echo "========================================"
