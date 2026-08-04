"""Isolated re-chunk/model experiments with quality-gated alias promotion."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
from typing import Mapping, Optional

from .config import Settings
from .content import page_to_chunks
from .qdrant import QdrantAdapter, versioned_collection
from .store import CanonicalStore


def _gate_is_meaningful(gate: Mapping[str, object], quality: Mapping[str, object]) -> bool:
    if gate.get("passed") is not True:
        return False
    criteria = gate.get("criteria")
    if not isinstance(criteria, Mapping) or not any(
        float(criteria.get(key, 0.0) or 0.0) > 0
        for key in ("min_recall", "min_mrr", "min_overlap")
    ):
        return False
    evaluated = int(quality.get("queries_evaluated", 0) or 0)
    total = int(quality.get("queries_total", evaluated) or 0)
    return evaluated > 0 and evaluated == total


def promotion_allowed(
    quality_report: Mapping[str, object], target_model_version: Optional[str] = None
) -> bool:
    providers = quality_report.get("providers")
    if isinstance(providers, list):
        candidates = [
            item
            for item in providers
            if isinstance(item, Mapping)
            and (
                target_model_version is None
                or str(item.get("model_version", "")) == target_model_version
            )
        ]
        return bool(candidates) and all(
            isinstance(item.get("quality_gate"), Mapping)
            and isinstance(item.get("quality"), Mapping)
            and _gate_is_meaningful(item["quality_gate"], item["quality"])
            for item in candidates
        )
    gate = quality_report.get("quality_gate")
    quality = quality_report.get("quality")
    return (
        isinstance(gate, Mapping)
        and isinstance(quality, Mapping)
        and _gate_is_meaningful(gate, quality)
    )


def validate_promotion_target(
    store: CanonicalStore,
    vector_store,
    *,
    model_version: str,
    chunker_version: str,
    collection_name: str,
) -> list[str]:
    failures = []
    chunks = int(store.drift_counts()["chunks"])
    unsettled = {
        row["status"]: int(row["count"])
        for row in store.connection.execute(
            "SELECT status,count(*) AS count FROM index_jobs "
            "WHERE model_version=? AND status IN ('pending','leased','dead') GROUP BY status",
            (model_version,),
        )
    }
    if unsettled:
        failures.append("target queue is not settled: %s" % unsettled)
    target = store.connection.execute(
        "SELECT collection_name,dimensions FROM index_targets WHERE model_version=?",
        (model_version,),
    ).fetchone()
    if target is None or str(target["collection_name"]) != collection_name:
        failures.append("target SQLite is not registered to collection %s" % collection_name)
    expected_dimensions = int(target["dimensions"]) if target is not None else None
    expected_identities = {
        (
            str(row["chunk_id"]),
            str(row["content_hash"]),
            model_version,
            str(row["chunker_version"]),
        )
        for row in store.connection.execute(
            "SELECT chunk_id,content_hash,chunker_version FROM chunks"
        )
    }
    if {identity[3] for identity in expected_identities} != {chunker_version}:
        failures.append("target SQLite chunker metadata does not match requested experiment")
    manifest = vector_store.collection_manifest()
    if not manifest.get("exists"):
        failures.append("target collection does not exist")
        return failures
    if int(manifest.get("count", -1)) != chunks:
        failures.append(
            "target collection count %s does not match SQLite chunks %s"
            % (manifest.get("count"), chunks)
        )
    actual_identity_rows = manifest.get("identities", [])
    actual_identities = {
        (
            str(item.get("chunk_id", "")),
            str(item.get("content_hash", "")),
            str(item.get("model_version", "")),
            str(item.get("chunker_version", "")),
        )
        for item in actual_identity_rows
        if isinstance(item, Mapping)
    }
    if len(actual_identities) != len(actual_identity_rows):
        failures.append("target collection contains duplicate or malformed point identities")
    if actual_identities != expected_identities:
        missing = expected_identities - actual_identities
        extra = actual_identities - expected_identities
        failures.append(
            "target collection identities do not match SQLite (missing=%d, extra=%d)"
            % (len(missing), len(extra))
        )
    if expected_dimensions is not None:
        if manifest.get("collection_dimensions") != [expected_dimensions]:
            failures.append("target collection vector dimensions do not match registration")
        point_dimensions = manifest.get("point_dimensions", [])
        if chunks and point_dimensions != [expected_dimensions]:
            failures.append("target point vector dimensions do not match registration")
    return failures


def build_experiment(
    source: CanonicalStore,
    settings: Settings,
    *,
    chunker_version: str,
    chunk_strategy: str,
    target_model_version: str,
    target: Optional[CanonicalStore] = None,
) -> dict:
    configured = replace(
        settings,
        chunker_version=chunker_version,
        chunk_strategy=chunk_strategy,
        model_version=target_model_version,
    )
    pages = source.connection.execute(
        "SELECT * FROM pages WHERE deleted_at IS NULL ORDER BY page_id"
    ).fetchall()
    page_count = 0
    chunk_count = 0
    skipped = []
    for row in pages:
        try:
            page = json.loads(row["source_json"])
        except (TypeError, json.JSONDecodeError):
            skipped.append(str(row["page_id"]))
            continue
        if not isinstance(page, dict) or not page.get("body"):
            skipped.append(str(row["page_id"]))
            continue
        chunks = page_to_chunks(page, str(row["section"]), configured)
        page_count += 1
        chunk_count += len(chunks)
        if target is not None:
            target.replace_page(
                {
                    "page_id": row["page_id"],
                    "section": row["section"],
                    "title": row["title"],
                    "url": row["url"],
                    "breadcrumbs": row["breadcrumbs"],
                    "confluence_version": row["confluence_version"],
                    "schema_version": configured.schema_version,
                    "source": page,
                    "source_text": row["source_text"],
                    "parent_text": row["parent_text"],
                },
                chunks,
                target_model_version,
            )
    return {
        "schema_version": "1",
        "dry_run": target is None,
        "source_pages": len(pages),
        "pages_rechunked": page_count,
        "chunks_generated": chunk_count,
        "pages_skipped_without_source": skipped,
        "chunker_version": chunker_version,
        "chunk_strategy": chunk_strategy,
        "target_model_version": target_model_version,
        "target_collection": versioned_collection(
            settings.qdrant_collection, target_model_version
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or build an isolated re-chunk/model experiment"
    )
    parser.add_argument("--base")
    parser.add_argument("--sqlite")
    parser.add_argument("--target-sqlite")
    parser.add_argument("--chunker-version", required=True)
    parser.add_argument("--chunk-strategy", default="auto")
    parser.add_argument("--target-model-version", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--quality-report")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    source_path = Path(args.sqlite or settings.sqlite_path).resolve()
    target_path = (
        Path(args.target_sqlite).expanduser().resolve() if args.target_sqlite else None
    )
    if (args.apply or args.promote) and target_path is None:
        parser.error("--apply/--promote requires --target-sqlite")
    if target_path == source_path:
        parser.error("target SQLite must differ from the active canonical store")
    if args.promote and not args.quality_report:
        parser.error("--promote requires --quality-report")
    target = (
        CanonicalStore(target_path) if (args.apply or args.promote) and target_path else None
    )
    target_chunks = target.drift_counts()["chunks"] if target is not None else 0
    if args.apply and target_chunks:
        target.close()
        parser.error("target SQLite must be empty; use a new experiment path")
    if args.promote and not args.apply and not target_chunks:
        target.close()
        parser.error("promotion target SQLite has no indexed experiment chunks")
    try:
        if args.promote and not args.apply:
            report = {
                "schema_version": "1",
                "dry_run": False,
                "chunks_generated": target_chunks,
                "chunker_version": args.chunker_version,
                "chunk_strategy": args.chunk_strategy,
                "target_model_version": args.target_model_version,
                "target_collection": versioned_collection(
                    settings.qdrant_collection, args.target_model_version
                ),
            }
        else:
            with CanonicalStore(source_path) as source:
                report = build_experiment(
                    source,
                    settings,
                    chunker_version=args.chunker_version,
                    chunk_strategy=args.chunk_strategy,
                    target_model_version=args.target_model_version,
                    target=target,
                )
    finally:
        if target is not None:
            target.close()
    report["target_sqlite"] = str(target_path) if target_path else None
    if args.quality_report:
        quality = json.loads(Path(args.quality_report).read_text(encoding="utf-8"))
        report["quality_gate_passed"] = promotion_allowed(
            quality, args.target_model_version
        )
    if args.promote:
        if not report.get("quality_gate_passed"):
            report["promotion"] = "blocked_quality_gate"
        else:
            collection = str(report["target_collection"])
            adapter = QdrantAdapter(
                settings.qdrant_url,
                collection,
                api_key=settings.qdrant_api_key,
                dimensions=settings.embedding_dimensions,
            )
            with CanonicalStore(target_path) as promotion_store:
                failures = validate_promotion_target(
                    promotion_store,
                    adapter,
                    model_version=args.target_model_version,
                    chunker_version=args.chunker_version,
                    collection_name=collection,
                )
            if failures:
                report["promotion"] = "blocked_target_validation"
                report["promotion_failures"] = failures
            else:
                report["previous_collection"] = adapter.promote_alias(
                    settings.qdrant_alias, collection
                )
                report["promotion"] = "completed"
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(output, encoding="utf-8")
    print(output, end="")
    return 2 if args.promote and report.get("promotion") != "completed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
