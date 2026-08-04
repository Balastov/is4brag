#!/usr/bin/env bash
# Install one incremental poll and one authoritative full inventory reconcile.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/sync_confluence.py" ]]; then
    # Deployed layout: all operational scripts live directly in KISU_BASE.
    DEFAULT_BASE="$SCRIPT_DIR"
else
    # Repository layout: scripts/ is one level below the project root.
    DEFAULT_BASE="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
BASE="${KISU_BASE:-$DEFAULT_BASE}"
PYTHON="${IS4BRAG_PYTHON:-$BASE/.venv/bin/python}"
SYNC="$BASE/scripts/sync_confluence.py"
[[ -f "$SYNC" ]] || SYNC="$BASE/sync_confluence.py"
LOCK="$BASE/.is4brag-sync.cron.lock"
LOG="$BASE/sync.log"
BEGIN="# BEGIN IS4BRAG MANAGED SYNC"
END="# END IS4BRAG MANAGED SYNC"

quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# Replace only this script's marked block. Every unrelated crontab entry is retained.
crontab -l 2>/dev/null | awk -v begin="$BEGIN" -v end="$END" '
    $0 == begin { managed=1; next }
    $0 == end { managed=0; next }
    !managed { print }
' >"$TMP" || true

{
    echo "$BEGIN"
    printf '0 21 * * * /usr/bin/flock -n %s %s %s --base %s --skip-index >> %s 2>&1\n' \
        "$(quote "$LOCK")" "$(quote "$PYTHON")" "$(quote "$SYNC")" \
        "$(quote "$BASE")" "$(quote "$LOG")"
    printf '30 3 * * 0 /usr/bin/flock -n %s %s %s --base %s --reconcile >> %s 2>&1\n' \
        "$(quote "$LOCK")" "$(quote "$PYTHON")" "$(quote "$SYNC")" \
        "$(quote "$BASE")" "$(quote "$LOG")"
    echo "$END"
} >>"$TMP"

crontab "$TMP"
echo "Installed managed incremental (daily) and inventory reconcile (weekly) jobs."
