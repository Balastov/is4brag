#!/usr/bin/env python3
"""Requeue every current chunk for an explicit, isolated model target."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from is4brag.config import Settings
from is4brag.qdrant import versioned_collection
from is4brag.store import CanonicalStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--sqlite")
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--dimensions", required=True, type=int)
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    collection = versioned_collection(settings.qdrant_collection, args.model_version)
    with CanonicalStore(args.sqlite or settings.sqlite_path) as store:
        store.register_index_target(
            args.model_version, args.runtime, args.dimensions, collection
        )
        jobs = store.requeue_model_version(args.model_version)
    print(
        json.dumps(
            {
                "model_version": args.model_version,
                "runtime": args.runtime,
                "dimensions": args.dimensions,
                "collection": collection,
                "jobs_requeued": jobs,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
