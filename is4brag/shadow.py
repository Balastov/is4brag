"""Compare legacy and canonical search backends against golden queries."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .benchmark import _matches, validate_golden
from .client import SearchClient


SearchBackend = Callable[[str, int, Optional[Sequence[str]]], list[dict]]


def _identity(item: Mapping) -> str:
    return str(item.get("page_id") or item.get("chunk_id") or item.get("url") or item.get("title"))


def _quality(results: Sequence[Mapping], expected: Mapping) -> float:
    return 1.0 if any(_matches(item, expected) for item in results) else 0.0


def compare_backends(
    legacy: SearchBackend,
    new: SearchBackend,
    golden: Mapping,
    *,
    top_k: int = 10,
    min_overlap: float = 0.0,
    max_quality_degradation: float = 0.0,
) -> dict:
    validate_golden(golden)
    comparisons = []
    for query in golden.get("queries", []):
        sections = [query["section"]] if query.get("section") else None
        old_results = legacy(str(query["query"]), top_k, sections)
        new_results = new(str(query["query"]), top_k, sections)
        old_ids = [_identity(item) for item in old_results[:top_k]]
        new_ids = [_identity(item) for item in new_results[:top_k]]
        overlap = len(set(old_ids).intersection(new_ids)) / float(max(top_k, 1))
        old_ranks = {item: index for index, item in enumerate(old_ids, 1)}
        new_ranks = {item: index for index, item in enumerate(new_ids, 1)}
        rank_differences = {
            item: new_ranks[item] - old_ranks[item]
            for item in set(old_ranks).intersection(new_ranks)
        }
        comparisons.append(
            {
                "id": query.get("id"),
                "query": query["query"],
                "overlap_at_k": overlap,
                "rank_differences": rank_differences,
                "legacy_quality": _quality(old_results, query.get("expected", {})),
                "new_quality": _quality(new_results, query.get("expected", {})),
            }
        )
    count = len(comparisons)
    overlap = sum(item["overlap_at_k"] for item in comparisons) / count if count else 0.0
    legacy_quality = sum(item["legacy_quality"] for item in comparisons) / count if count else 0.0
    new_quality = sum(item["new_quality"] for item in comparisons) / count if count else 0.0
    failures = []
    if overlap < min_overlap:
        failures.append("overlap_at_k %.6f is below %.6f" % (overlap, min_overlap))
    degradation = legacy_quality - new_quality
    if degradation > max_quality_degradation:
        failures.append(
            "golden quality degradation %.6f exceeds %.6f"
            % (degradation, max_quality_degradation)
        )
    return {
        "k": top_k,
        "queries": comparisons,
        "summary": {
            "overlap_at_k": overlap,
            "legacy_quality": legacy_quality,
            "new_quality": new_quality,
        },
        "quality_gate": {"passed": not failures, "failures": failures},
    }


def _load_legacy(path: Path):
    spec = importlib.util.spec_from_file_location("is4brag_legacy_search", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load legacy search script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Shadow-compare legacy and search API results")
    parser.add_argument("--legacy-script", default="skills/kisu-metro/scripts/kisu_metro_search.py")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--golden", default="fixtures/golden_queries.json")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-overlap", type=float, default=0.0)
    parser.add_argument("--max-quality-degradation", type=float, default=0.0)
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    legacy_module = _load_legacy(Path(args.legacy_script))
    client = SearchClient(args.api_url)

    def legacy(query, top_k, sections):
        # Explicit legacy entrypoint avoids SEARCH_API_URL recursion.
        return legacy_module.legacy_search(query, top_k=top_k, sections=sections)

    def new(query, top_k, sections):
        return client.search(query, top_k=top_k, sections=sections)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    validate_golden(golden)
    report = compare_backends(
        legacy,
        new,
        golden,
        top_k=args.top_k,
        min_overlap=args.min_overlap,
        max_quality_degradation=args.max_quality_degradation,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["quality_gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
