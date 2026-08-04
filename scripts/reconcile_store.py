#!/usr/bin/env python3
"""Report canonical/vector drift without mutating either store."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from is4brag.config import Settings
from is4brag.qdrant import QdrantAdapter
from is4brag.store import CanonicalStore


def resolve_vector_collection(settings, store, adapter_factory=QdrantAdapter):
    probe = adapter_factory(
        settings.qdrant_url,
        settings.qdrant_alias or settings.qdrant_collection,
        api_key=settings.qdrant_api_key,
        dimensions=settings.embedding_dimensions,
    )
    if settings.qdrant_alias:
        target = probe.alias_target(settings.qdrant_alias)
        if target:
            return target
    row = store.connection.execute(
        "SELECT collection_name FROM index_targets WHERE model_version=?",
        (settings.model_version,),
    ).fetchone()
    if row:
        return str(row["collection_name"])
    raise RuntimeError(
        "no active alias or registered collection for model %s" % settings.model_version
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report SQLite/Qdrant drift")
    parser.add_argument("--base")
    parser.add_argument("--sqlite")
    parser.add_argument("--no-qdrant", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    with CanonicalStore(args.sqlite or settings.sqlite_path) as store:
        vectors = None
        resolve_error = None
        if not args.no_qdrant:
            try:
                collection = resolve_vector_collection(settings, store)
                vectors = QdrantAdapter(
                    settings.qdrant_url,
                    collection,
                    api_key=settings.qdrant_api_key,
                    dimensions=settings.embedding_dimensions,
                )
            except Exception as exc:
                resolve_error = str(exc)
        vector_count = None
        error = resolve_error
        if vectors:
            try:
                vector_count = vectors.count()
            except Exception as exc:
                error = str(exc)
        report = {
            "drift": store.drift_counts(vector_count),
            "queue": store.queue_metrics(),
            "qdrant_error": error,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if error is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
