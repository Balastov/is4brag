"""Import legacy JSONL/NumPy artifacts without refetching Confluence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from collections import Counter
from typing import Optional

from .config import Settings
from .content import content_hash, stable_chunk_id
from .store import CanonicalStore


def _load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def normalize_legacy_chunk(chunk: dict, section: str, ordinal: int) -> dict:
    result = dict(chunk)
    text = str(result.get("text", ""))
    digest = content_hash(text)
    page_id = str(result.get("page_id", ""))
    result.update(
        {
            "chunk_id": str(
                result.get("chunk_id")
                if isinstance(result.get("chunk_id"), str)
                else stable_chunk_id(section, page_id, digest, ordinal)
            ),
            "page_id": page_id,
            "section": str(result.get("section") or section),
            "content_hash": digest,
            "chunk_index": int(result.get("chunk_index", ordinal)),
            "duplicate_ordinal": int(result.get("duplicate_ordinal", 0)),
            "chunker_version": str(result.get("chunker_version", "legacy")),
            "schema_version": str(result.get("schema_version", "legacy")),
        }
    )
    return result


def import_section(
    store: CanonicalStore,
    section_dir: Path,
    model_version: str,
    *,
    expected_dimensions: int = 1024,
) -> dict:
    chunks_path = section_dir / "chunks_export.jsonl"
    if not chunks_path.exists():
        raise FileNotFoundError(chunks_path)
    raw_chunks = _load_jsonl(chunks_path)
    chunks = [
        normalize_legacy_chunk(chunk, section_dir.name, index)
        for index, chunk in enumerate(raw_chunks)
    ]
    by_page: dict[str, list[dict]] = {}
    for chunk in chunks:
        by_page.setdefault(chunk["page_id"], []).append(chunk)
    for page_id, page_chunks in by_page.items():
        first = page_chunks[0]
        store.replace_page(
            {
                "page_id": page_id,
                "section": first["section"],
                "title": first.get("title", ""),
                "url": first.get("url", ""),
                "breadcrumbs": first.get("breadcrumbs", ""),
                "schema_version": first["schema_version"],
                "source": {"legacy_import": str(chunks_path)},
            },
            page_chunks,
            model_version,
        )

    cached = 0
    reason: Optional[str] = None
    embeddings_path = section_dir / "embeddings.npy"
    meta_path = section_dir / "index_meta.json"
    index_path = section_dir / "chunks_index.json"
    if embeddings_path.exists():
        try:
            import numpy as np
        except ImportError:
            reason = "numpy unavailable"
        else:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            legacy_model = str(meta.get("embedding_model", ""))
            vectors = np.load(str(embeddings_path), mmap_mode="r")
            index_records = (
                json.loads(index_path.read_text(encoding="utf-8"))
                if index_path.exists()
                else chunks
            )
            if legacy_model and legacy_model != model_version:
                reason = "model mismatch: %s" % legacy_model
            elif vectors.ndim != 2 or vectors.shape[0] != len(index_records):
                reason = "embedding row count mismatch"
            elif int(vectors.shape[1]) != expected_dimensions:
                reason = "embedding dimension mismatch"
            elif Counter(
                (str(item.get("page_id", "")), str(item.get("text", "")))
                for item in index_records
            ) != Counter((item["page_id"], item["text"]) for item in chunks):
                reason = "chunks_index metadata mismatch"
            else:
                hashes_by_record: dict[tuple[str, str], list[str]] = {}
                for chunk in chunks:
                    key = (chunk["page_id"], chunk["text"])
                    hashes_by_record.setdefault(key, []).append(chunk["content_hash"])
                for indexed_chunk, vector in zip(index_records, vectors):
                    key = (
                        str(indexed_chunk.get("page_id", "")),
                        str(indexed_chunk.get("text", "")),
                    )
                    store.put_embedding(
                        hashes_by_record[key].pop(),
                        model_version,
                        vector.tolist(),
                    )
                    cached += 1
    return {
        "section": section_dir.name,
        "pages": len(by_page),
        "chunks": len(chunks),
        "embeddings_cached": cached,
        "embeddings_skipped": reason,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import legacy chunks and embeddings")
    parser.add_argument("--base", required=True)
    parser.add_argument("--section", action="append")
    parser.add_argument("--sqlite")
    parser.add_argument("--dimensions", type=int, default=1024)
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    base = Path(args.base)
    sections = args.section or [
        path.name for path in base.iterdir() if (path / "chunks_export.jsonl").exists()
    ]
    with CanonicalStore(args.sqlite or settings.sqlite_path) as store:
        for section in sections:
            report = import_section(
                store,
                base / section,
                settings.model_version,
                expected_dimensions=args.dimensions,
            )
            print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
