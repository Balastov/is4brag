"""Per-section synchronization state with legacy-state migration."""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Dict, Iterable, Optional

from .io import atomic_write_json

STATE_FORMAT_VERSION = 3


def empty_section_state() -> dict:
    return {
        "last_sync": None,
        "page_versions": {},
        "inventory": [],
        "ownership_known": False,
    }


def migrate_state(raw: object, sections: Iterable[str]) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    if isinstance(raw.get("sections"), dict):
        result = dict(raw)
        result["state_format_version"] = STATE_FORMAT_VERSION
        result["sections"] = dict(result["sections"])
        unsafe_legacy_split = bool(result.get("migrated_from_global")) and not all(
            isinstance(value, dict) and "ownership_known" in value
            for value in result["sections"].values()
        )
        for section in sections:
            current = result["sections"].setdefault(section, empty_section_state())
            if unsafe_legacy_split:
                # Version 2 copied one global inventory into every section. Its
                # ownership cannot be reconstructed safely, so force a fresh
                # per-section inventory before deletion is allowed.
                current.update(empty_section_state())
            else:
                current.setdefault("last_sync", None)
                current.setdefault("page_versions", {})
                current.setdefault("inventory", [])
                current.setdefault("ownership_known", False)
        return result

    return {
        "state_format_version": STATE_FORMAT_VERSION,
        "migrated_from_global": bool(raw),
        "legacy_checkpoint": {
            "last_sync": raw.get("last_sync"),
            "page_count": len(raw.get("page_versions", {})),
        }
        if raw
        else None,
        # A global legacy map has no reliable section ownership. Starting each
        # section empty causes a safe full refresh instead of cross-section deletes.
        "sections": {section: empty_section_state() for section in sections},
    }


def load_state(base: str, sections: Iterable[str]) -> dict:
    path = Path(base) / "sync_state.json"
    if not path.exists():
        return migrate_state({}, sections)
    try:
        with path.open(encoding="utf-8") as handle:
            return migrate_state(json.load(handle), sections)
    except (OSError, ValueError):
        raise ValueError("invalid sync state: %s" % path)


def section_state(state: dict, section: str) -> dict:
    sections: Dict[str, dict] = state.setdefault("sections", {})
    current = sections.setdefault(section, empty_section_state())
    current.setdefault("last_sync", None)
    current.setdefault("page_versions", {})
    current.setdefault("inventory", [])
    current.setdefault("ownership_known", False)
    return current


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def save_state(
    base: str,
    state: dict,
    completed_section: Optional[str] = None,
    checkpoint: Optional[str] = None,
) -> None:
    if completed_section:
        section_state(state, completed_section)["last_sync"] = checkpoint or utc_now()
    state["state_format_version"] = STATE_FORMAT_VERSION
    state["updated_at"] = utc_now()
    atomic_write_json(Path(base) / "sync_state.json", state)
