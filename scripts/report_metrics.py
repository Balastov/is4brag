#!/usr/bin/env python3
"""Report corpus, local-index, search, and pending-work metrics as JSON."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Dict


def _read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def collect_metrics(base: Path) -> dict:
    base = Path(base)
    pending = _read_json(base / "reindex_pending.json", {})
    sections: Dict[str, dict] = {}
    total_chunks = 0
    total_pages = 0
    indexed_chunks = 0
    for chunks_path in sorted(base.glob("*/chunks_export.jsonl")):
        page_ids = set()
        chunks = 0
        try:
            with chunks_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    chunks += 1
                    page_ids.add(str(record.get("page_id", "")))
        except (OSError, ValueError):
            continue
        section = chunks_path.parent.name
        index_meta = _read_json(chunks_path.parent / "index_meta.json", {})
        section_indexed = int(index_meta.get("total_chunks", 0) or 0)
        sections[section] = {
            "corpus_chunks": chunks,
            "corpus_pages": len(page_ids),
            "indexed_chunks": section_indexed,
            "pending_pages": len(pending.get(section, [])),
        }
        total_chunks += chunks
        total_pages += len(page_ids)
        indexed_chunks += section_indexed

    search = _read_json(base / "search_metrics.json", {})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": str(base),
        "corpus": {"chunks": total_chunks, "pages": total_pages},
        "index": {"chunks": indexed_chunks, "coverage": indexed_chunks / total_chunks if total_chunks else 0},
        "search": search,
        "pending": {
            "sections": len([value for value in pending.values() if value]),
            "pages": sum(len(value) for value in pending.values()),
        },
        "sections": sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=".", help="Runtime data directory")
    args = parser.parse_args()
    print(json.dumps(collect_metrics(Path(args.base)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
