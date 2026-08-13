#!/usr/bin/env python3
"""Re-fetch Confluence pages and rewrite chunks with the current table parser.

Example:
  .venv/bin/python rechunk_table_pages.py --pages 12366437
  .venv/bin/python rechunk_table_pages.py --auto 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from is4brag.config import Settings
from is4brag.content import normalize_html, page_to_chunks
from is4brag.ingest import ConfluenceClient
from is4brag.store import CanonicalStore


def _pick_auto(store: CanonicalStore, limit: int, exclude: set[str]) -> list[str]:
    rows = store.connection.execute(
        """
        SELECT c.page_id AS page_id, p.title AS title, count(*) AS n
        FROM chunks c
        JOIN pages p ON p.page_id = c.page_id
        WHERE c.text LIKE '%[Таблица %'
          AND p.deleted_at IS NULL
        GROUP BY c.page_id
        ORDER BY n DESC
        LIMIT ?
        """,
        (max(limit * 3, limit),),
    ).fetchall()
    chosen: list[str] = []
    for row in rows:
        page_id = str(row["page_id"])
        if page_id in exclude:
            continue
        chosen.append(page_id)
        print(
            "auto-pick",
            page_id,
            row["title"],
            "old_table_chunks=",
            row["n"],
            flush=True,
        )
        if len(chosen) >= limit:
            break
    return chosen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="KISU Metro base path")
    parser.add_argument(
        "--pages",
        nargs="*",
        default=[],
        help="Explicit Confluence page ids",
    )
    parser.add_argument(
        "--auto",
        type=int,
        default=0,
        help="Also pick N pages that already contain [Таблица in chunks",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="Poll index queue this many seconds after enqueue",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env(args.base)
    print("chunker_version", settings.chunker_version, flush=True)
    if not str(settings.chunker_version).startswith("4"):
        print(
            "WARNING: expected IS4BRAG_CHUNKER_VERSION=4, got %r"
            % (settings.chunker_version,),
            flush=True,
        )

    store = CanonicalStore(settings.sqlite_path)
    client = ConfluenceClient(settings)

    page_ids: list[str] = []
    seen: set[str] = set()
    for page_id in args.pages:
        page_id = str(page_id).strip()
        if page_id and page_id not in seen:
            page_ids.append(page_id)
            seen.add(page_id)
    if args.auto:
        for page_id in _pick_auto(store, args.auto, seen):
            page_ids.append(page_id)
            seen.add(page_id)
    if not page_ids:
        page_ids = ["12366437"]
        print("default page", page_ids[0], flush=True)

    for page_id in page_ids:
        page = client.fetch_page(page_id)
        section_row = store.connection.execute(
            "SELECT section FROM pages WHERE page_id=?", (page_id,)
        ).fetchone()
        section_name = section_row["section"] if section_row else "Стадии проекта"
        body = page.get("body", {}).get("storage", {}).get("value", "")
        document = normalize_html(body)
        table_markers = [
            line
            for line in document.text.splitlines()
            if line.startswith("[Таблица ") or line.startswith("| строка")
        ]
        print(flush=True)
        print("===", page_id, page.get("title"), "section=", section_name, flush=True)
        print(
            "tables_in_norm",
            sum(1 for line in document.text.splitlines() if line.startswith("[Таблица ")),
            flush=True,
        )
        print("sample_rows:", flush=True)
        for line in table_markers[:8]:
            print(" ", line[:180], flush=True)

        chunks = page_to_chunks(page, section_name, settings)
        store.replace_page(
            {
                "page_id": page_id,
                "section": section_name,
                "title": page.get("title", ""),
                "url": "%s/spaces/METRO/pages/%s" % (settings.confluence_url, page_id),
                "breadcrumbs": "",
                "confluence_version": page.get("version", {}).get("number", 0),
                "schema_version": settings.schema_version,
                "source": {"manual_table_rechunk": True},
                "source_text": document.text,
                "parent_text": document.text,
            },
            chunks,
            settings.model_version,
        )
        labeled = sum(
            1
            for chunk in chunks
            if chunk.get("content_type") == "table" and ": " in chunk.get("text", "")
        )
        print(
            "chunks_written",
            len(chunks),
            "chunker",
            sorted({chunk["chunker_version"] for chunk in chunks}),
            "table_chunks_with_col_labels",
            labeled,
            flush=True,
        )

    print("queue", store.queue_metrics(), flush=True)

    if args.wait_seconds > 0:
        import time

        deadline = time.time() + args.wait_seconds
        while time.time() < deadline:
            metrics = store.queue_metrics()
            print("queue", metrics, flush=True)
            if metrics.get("pending", 0) == 0 and metrics.get("leased", 0) == 0:
                break
            time.sleep(min(15, max(1, args.wait_seconds // 6)))

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
