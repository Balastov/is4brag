#!/usr/bin/env python3
"""Safely roll back a Qdrant collection alias."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from is4brag.config import Settings
from is4brag.qdrant import QdrantAdapter


def guarded_rollback(adapter, alias, target_collection, expected_current_collection):
    """Roll back only from the explicitly observed active collection."""
    current = adapter.alias_target(alias)
    if current is None:
        raise RuntimeError("active alias does not exist: %s" % alias)
    if current != expected_current_collection:
        raise RuntimeError(
            "active alias changed: expected %s, found %s"
            % (expected_current_collection, current)
        )
    if target_collection == current:
        raise ValueError("rollback target is already active")
    return adapter.rollback_alias(alias, target_collection)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("rollback",))
    parser.add_argument("--base")
    parser.add_argument("--alias")
    parser.add_argument("--target-collection", required=True)
    parser.add_argument("--expected-current-collection", required=True)
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    collection = args.target_collection
    alias = args.alias or settings.qdrant_alias
    adapter = QdrantAdapter(
        settings.qdrant_url,
        collection,
        api_key=settings.qdrant_api_key,
        dimensions=settings.embedding_dimensions,
    )
    previous = guarded_rollback(
        adapter,
        alias,
        collection,
        args.expected_current_collection,
    )
    print(
        json.dumps(
            {"action": args.action, "alias": alias, "target": collection, "previous": previous}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
