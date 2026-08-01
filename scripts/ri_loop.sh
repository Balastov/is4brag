#!/bin/bash
set -euo pipefail
SECTION="${1:-}"
PYTHON="/app/backend/.deer-flow/users/e1e1bb08-dff7-4978-a378-c6a585a6a0fb/threads/e9a1a16a-5832-4326-bb1b-4bde19665b5e/user-data/workspace/.venv/bin/python"
INDEXER="/mnt/write/KISU Metro/resumable_index.py"
LOG_DIR="/mnt/write/KISU Metro/logs"
if [ -z "$SECTION" ]; then echo "Usage: ri_loop.sh SECTION"; exit 1; fi
[ ! -f "$INDEXER" ] && { echo "Not found: $INDEXER"; exit 1; }
mkdir -p "$LOG_DIR"
SAFE=$(echo "$SECTION" | tr " " "_" | tr -d "\"")
LOGFILE="$LOG_DIR/index_${SAFE}_$(date +%Y%m%d_%H%M%S).log"
export HF_HOME="${HF_HOME:-/app/backend/.deer-flow/users/e1e1bb08-dff7-4978-a378-c6a585a6a0fb/threads/e9a1a16a-5832-4326-bb1b-4bde19665b5e/user-data/workspace/.hf_cache}"
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NUMEXPR_NUM_THREADS=4
echo "==== $(date) ==== $SECTION ====" | tee -a "$LOGFILE"
RUN=0
while true; do
  RUN=$((RUN + 1))
  echo "--- Run #$RUN $(date) ---" | tee -a "$LOGFILE"
  set +e
  "$PYTHON" "$INDEXER" "$SECTION" >> "$LOGFILE" 2>&1
  EC=$?
  set -e
  if [ "$EC" -eq 0 ]; then echo "DONE: $SECTION in $RUN runs" | tee -a "$LOGFILE"; exit 0
  elif [ "$EC" -eq 2 ]; then echo "Continue..." | tee -a "$LOGFILE"; sleep 1
  else echo "ERROR exit=$EC LOG=$LOGFILE" | tee -a "$LOGFILE"; exit $EC; fi
done
