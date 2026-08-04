"""Inventory reconciliation and deletion tombstones."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .io import atomic_write_jsonl


def reconcile_chunks(
    chunks: Sequence[Mapping[str, object]],
    inventory_page_ids: Iterable[str],
    known_page_ids: Iterable[str] = (),
) -> Tuple[List[Mapping[str, object]], Set[str]]:
    inventory = {str(page_id) for page_id in inventory_page_ids}
    existing = (
        {str(chunk.get("page_id", "")) for chunk in chunks}
        | {str(page_id) for page_id in known_page_ids}
    )
    stale = existing - inventory
    kept = [chunk for chunk in chunks if str(chunk.get("page_id", "")) not in stale]
    return kept, stale


def make_tombstones(
    section: str,
    page_ids: Iterable[str],
    deleted_at: Optional[str] = None,
) -> List[dict]:
    timestamp = deleted_at or datetime.now(timezone.utc).isoformat()
    return [
        {
            "section": section,
            "page_id": str(page_id),
            "deleted_at": timestamp,
            "reason": "missing_from_confluence_inventory",
            "schema_version": "1",
        }
        for page_id in sorted(set(page_ids))
    ]


def append_tombstones(path: Path, tombstones: Sequence[Mapping[str, object]]) -> None:
    if not tombstones:
        return
    existing: List[Mapping[str, object]] = []
    path = Path(path)
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            existing = [json.loads(line) for line in handle if line.strip()]
    atomic_write_jsonl(path, existing + list(tombstones))
