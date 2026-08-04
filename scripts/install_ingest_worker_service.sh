#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -d "$SCRIPT_DIR/systemd" ]]; then
    REPO_ROOT="$SCRIPT_DIR"
fi
UNIT_DEST="${UNIT_DEST:-/etc/systemd/system/is4brag-ingest-event-worker.service}"
ENV_DEST="${ENV_DEST:-/etc/is4brag/ingest-worker.env}"
BASE="${KISU_BASE:-$REPO_ROOT}"
PYTHON="${IS4BRAG_PYTHON:-$BASE/.venv/bin/python}"
SERVICE_USER="${IS4BRAG_SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${IS4BRAG_SERVICE_GROUP:-$(id -gn)}"

[[ -x "$PYTHON" ]] || { echo "Python not found: $PYTHON" >&2; exit 1; }

sudo install -d -m 0755 "$(dirname "$ENV_DEST")"
if [[ ! -e "$ENV_DEST" ]]; then
    sed "s|/var/lib/is4brag|$BASE|g" \
        "$REPO_ROOT/systemd/ingest-worker.env.example" | sudo tee "$ENV_DEST" >/dev/null
    sudo chmod 0640 "$ENV_DEST"
    echo "Created $ENV_DEST; edit paths and credentials before starting."
fi
sed \
    -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
    -e "s|@SERVICE_GROUP@|$SERVICE_GROUP|g" \
    -e "s|@ENV_PATH@|$ENV_DEST|g" \
    -e "s|@BASE_PATH@|$BASE|g" \
    -e "s|@PYTHON_PATH@|$PYTHON|g" \
    "$REPO_ROOT/systemd/is4brag-ingest-event-worker.service" | sudo tee "$UNIT_DEST" >/dev/null
sudo chmod 0644 "$UNIT_DEST"
sudo systemctl daemon-reload
sudo systemctl enable is4brag-ingest-event-worker.service
echo "Installed but not started. After editing $ENV_DEST run:"
echo "  sudo systemctl start is4brag-ingest-event-worker.service"
