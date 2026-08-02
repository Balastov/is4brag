#!/usr/bin/env bash
# deploy.sh — выкладка кода is4brag на хост DeerFlow / KISU Metro.
# Не трогает .env, .venv, чанки и индексы.
#
# Запуск из корня клона или из scripts/:
#   bash scripts/deploy.sh
#   KISU_BASE="..." SKILLS_PUBLIC="..." bash scripts/deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

KISU_BASE="${KISU_BASE:-/home/alex/Desktop/DeerFlow/WRITE_FOLDER/KISU Metro}"
SKILLS_PUBLIC="${SKILLS_PUBLIC:-}"
DEPLOY_LOG_TAG="${DEPLOY_LOG_TAG:-deploy}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$DEPLOY_LOG_TAG] $*"; }

die() { log "ERROR: $*"; exit 1; }

detect_skills_public() {
    if [[ -n "${SKILLS_PUBLIC}" && -d "${SKILLS_PUBLIC}" ]]; then
        echo "${SKILLS_PUBLIC}"
        return
    fi
    local candidates=(
        "/home/alex/Desktop/DeerFlow/skills/public"
        "/home/alex/Desktop/DeerFlow/WRITE_FOLDER/skills/public"
        "/app/skills/public"
        "/mnt/skills/public"
    )
    local c
    for c in "${candidates[@]}"; do
        if [[ -d "$c" ]]; then
            echo "$c"
            return
        fi
    done
    # Автопоиск по уже установленному skill
    local found
    found="$(find /home/alex/Desktop/DeerFlow /app /mnt -path '*/skills/public/kisu-metro/SKILL.md' 2>/dev/null | head -1 || true)"
    if [[ -n "$found" ]]; then
        dirname "$(dirname "$found")"
        return
    fi
    echo ""
}

[[ -d "$REPO_ROOT/scripts" ]] || die "не найден $REPO_ROOT/scripts"
[[ -d "$REPO_ROOT/skills" ]] || die "не найден $REPO_ROOT/skills"
[[ -d "$KISU_BASE" ]] || die "KISU_BASE не существует: $KISU_BASE"

log "REPO=$REPO_ROOT"
log "KISU_BASE=$KISU_BASE"

# ── 1. Инфраструктурные скрипты → плоский layout KISU Metro ──
SCRIPTS_COPY=(
    sync_confluence.py
    index_section.py
    resumable_index.py
    index_all.sh
    ri_loop.sh
    ri_loop_host.sh
    setup_cron.sh
    deploy.sh
)

for f in "${SCRIPTS_COPY[@]}"; do
    src="$REPO_ROOT/scripts/$f"
    [[ -f "$src" ]] || continue
    # install: сохраняет содержимое, обновляет mtime; sudo если нет прав
    if [[ -w "$KISU_BASE" ]] && { [[ ! -e "$KISU_BASE/$f" ]] || [[ -w "$KISU_BASE/$f" ]]; }; then
        install -m 0644 "$src" "$KISU_BASE/$f"
    else
        sudo install -m 0644 -o "$(id -u)" -g "$(id -g)" "$src" "$KISU_BASE/$f"
    fi
    # shell-скрипты — исполняемые
    if [[ "$f" == *.sh ]]; then
        chmod +x "$KISU_BASE/$f" 2>/dev/null || sudo chmod +x "$KISU_BASE/$f"
    fi
    log "→ KISU: $f"
done

# ── 2. Skills → DeerFlow skills/public ──
SKILLS_DEST="$(detect_skills_public)"
if [[ -z "$SKILLS_DEST" ]]; then
    log "WARN: skills/public не найден — скрипты KISU обновлены, skills пропущены"
    log "      Задайте SKILLS_PUBLIC=/path/to/skills/public"
else
    log "SKILLS_PUBLIC=$SKILLS_DEST"
    for skill in kisu-metro galaktika-erp; do
        src="$REPO_ROOT/skills/$skill"
        [[ -d "$src" ]] || continue
        dest="$SKILLS_DEST/$skill"
        mkdir -p "$dest" 2>/dev/null || sudo mkdir -p "$dest"
        if command -v rsync >/dev/null 2>&1; then
            if [[ -w "$SKILLS_DEST" ]]; then
                rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$src/" "$dest/"
            else
                sudo rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$src/" "$dest/"
            fi
        else
            # fallback без rsync
            if [[ -w "$SKILLS_DEST" ]]; then
                rm -rf "$dest"
                mkdir -p "$dest"
                cp -a "$src/." "$dest/"
            else
                sudo rm -rf "$dest"
                sudo mkdir -p "$dest"
                sudo cp -a "$src/." "$dest/"
            fi
        fi
        log "→ SKILL: $skill"
    done
fi

# ── 3. Быстрая проверка синтаксиса ──
VENV_PY="$KISU_BASE/.venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
    "$VENV_PY" -m py_compile "$KISU_BASE/sync_confluence.py" \
        "$KISU_BASE/index_section.py" \
        "$KISU_BASE/resumable_index.py"
    log "py_compile OK"
else
    log "WARN: нет $VENV_PY — py_compile пропущен"
fi

if [[ -n "$SKILLS_DEST" && -f "$SKILLS_DEST/kisu-metro/scripts/kisu_metro_search.py" ]]; then
    if grep -q 'E5_QUERY_PREFIX\|query:' "$SKILLS_DEST/kisu-metro/scripts/kisu_metro_search.py"; then
        log "kisu_metro_search: E5 prefixes OK"
    fi
    if grep -q 'PAGE_BATCH_SIZE' "$KISU_BASE/sync_confluence.py"; then
        log "sync_confluence: batch/resumable OK"
    fi
fi

log "DONE rev=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
