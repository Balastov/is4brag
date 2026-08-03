#!/bin/bash
# ─── ri_loop_host.sh ─── Версия для запуска НА СЕРВЕРЕ (master@rag) ───
# Использование:
#   chmod +x ri_loop_host.sh
#   ./ri_loop_host.sh "Стадии проекта"
#   ./ri_loop_host.sh "Управление проектом"
#   nohup ./ri_loop_host.sh "Стадии проекта" > /dev/null 2>&1 &   ← фон
# ────────────────────────────────────────────────────────────────────
set -euo pipefail

SECTION="${1:-}"
BASE="/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro"

# --- Проверки ---
if [ -z "$SECTION" ]; then
    echo "Укажите раздел:  ./ri_loop_host.sh \"Стадии проекта\""
    exit 1
fi

# --- Пути (все внутри $BASE) ---
PYTHON="$BASE/.venv/bin/python"
INDEXER="$BASE/resumable_index.py"
LOG_DIR="$BASE/logs"
HF_CACHE="$BASE/.hf_cache"          # кэш модели — скачается 1 раз

if [ ! -f "$PYTHON" ]; then
    echo "ОШИБКА: не найден python: $PYTHON"
    echo "Создайте venv:  cd \"$BASE\" && python3 -m venv .venv"
    exit 1
fi

if [ ! -f "$INDEXER" ]; then
    echo "ОШИБКА: не найден скрипт: $INDEXER"
    exit 1
fi

# --- Подготовка ---
mkdir -p "$LOG_DIR"
mkdir -p "$HF_CACHE"
SAFE=$(echo "$SECTION" | tr ' /' '_' | tr -d '"')
LOGFILE="$LOG_DIR/index_${SAFE}_$(date +%Y%m%d_%H%M%S).log"

export KISU_METRO_BASE="$BASE"
export HF_HOME="$HF_CACHE"
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4
export NUMEXPR_NUM_THREADS=4

# --- Главный цикл ---
echo "=========================================" | tee -a "$LOGFILE"
echo "  $(date) — СТАРТ: $SECTION"             | tee -a "$LOGFILE"
echo "  LOG: $LOGFILE"                          | tee -a "$LOGFILE"
echo "=========================================" | tee -a "$LOGFILE"

RUN=0
while true; do
    RUN=$((RUN + 1))
    echo ""                                      | tee -a "$LOGFILE"
    echo "───── Запуск #$RUN  $(date) ─────"     | tee -a "$LOGFILE"

    set +e
    "$PYTHON" "$INDEXER" "$SECTION" >> "$LOGFILE" 2>&1
    EC=$?
    set -e

    if [ "$EC" -eq 0 ]; then
        echo ""                                  | tee -a "$LOGFILE"
        echo "=========================================" | tee -a "$LOGFILE"
        echo "  ГОТОВО! $SECTION завершён за $RUN запусков" | tee -a "$LOGFILE"
        echo "  $(date)"                          | tee -a "$LOGFILE"
        echo "=========================================" | tee -a "$LOGFILE"
        exit 0

    elif [ "$EC" -eq 2 ]; then
        echo "  → Выход по таймауту, продолжаем..." | tee -a "$LOGFILE"
        sleep 1

    else
        echo ""                                  | tee -a "$LOGFILE"
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" | tee -a "$LOGFILE"
        echo "  ОШИБКА! Код выхода: $EC"          | tee -a "$LOGFILE"
        echo "  Лог: $LOGFILE"                    | tee -a "$LOGFILE"
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!" | tee -a "$LOGFILE"
        exit "$EC"
    fi
done
