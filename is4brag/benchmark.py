"""Offline-capable embedding benchmark and golden-query quality gate."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
import resource
import sqlite3
import statistics
import time
from typing import Mapping, Optional, Sequence

from .config import Settings
from .worker import build_provider


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * percent / 100.0
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


RELEVANCE_FIELDS = ("page_ids", "title_contains", "answer_contains")


def validate_golden(golden: Mapping) -> None:
    queries = golden.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("golden fixture must contain at least one query")
    seen = set()
    for item in queries:
        if not isinstance(item, Mapping):
            raise ValueError("golden query must be an object")
        query_id = str(item.get("id", ""))
        if not query_id or query_id in seen:
            raise ValueError("golden query IDs must be non-empty and unique")
        seen.add(query_id)
        expected = item.get("expected")
        if not isinstance(expected, Mapping) or not any(expected.get(key) for key in RELEVANCE_FIELDS):
            raise ValueError("golden query %s has no relevance assertion" % query_id)
        for key in RELEVANCE_FIELDS:
            values = expected.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError("golden query %s has invalid %s" % (query_id, key))
            if any(
                "placeholder" in value.lower()
                or (value.startswith("<") and value.endswith(">"))
                for value in values
            ):
                raise ValueError("golden query %s contains placeholder values" % query_id)


def retrieval_metrics(
    rankings: Sequence[Sequence[str]],
    relevant: Sequence[set[str]],
    k: int,
) -> dict:
    if len(rankings) != len(relevant):
        raise ValueError("rankings and relevant query counts differ")
    recalls = []
    reciprocals = []
    overlaps = []
    for ranking, expected in zip(rankings, relevant):
        if not expected:
            continue
        top = list(ranking[:k])
        recalls.append(len(expected.intersection(top)) / float(len(expected)))
        union = expected.union(top)
        overlaps.append(len(expected.intersection(top)) / float(len(union) or 1))
        rank = next((index for index, item in enumerate(top, 1) if item in expected), None)
        reciprocals.append(0.0 if rank is None else 1.0 / rank)
    return {
        "queries_evaluated": len(recalls),
        "queries_total": len(rankings),
        "recall_at_k": statistics.mean(recalls) if recalls else 0.0,
        "mrr": statistics.mean(reciprocals) if reciprocals else 0.0,
        "overlap_at_k": statistics.mean(overlaps) if overlaps else 0.0,
        "k": k,
    }


def quality_gate(
    metrics: Mapping[str, float],
    *,
    min_recall: float = 0.0,
    min_mrr: float = 0.0,
    min_overlap: float = 0.0,
    max_latency_ms: Optional[float] = None,
    baseline: Optional[Mapping[str, float]] = None,
    max_degradation: float = 0.0,
) -> dict:
    failures = []
    evaluated = int(metrics.get("queries_evaluated", 0))
    total = int(metrics.get("queries_total", evaluated))
    if evaluated == 0:
        failures.append("zero golden queries were evaluated")
    elif evaluated != total:
        failures.append("%d of %d golden queries had relevant corpus records" % (evaluated, total))
    for key, minimum in (
        ("recall_at_k", min_recall),
        ("mrr", min_mrr),
        ("overlap_at_k", min_overlap),
    ):
        actual = float(metrics.get(key, 0.0))
        if actual < minimum:
            failures.append("%s %.6f is below %.6f" % (key, actual, minimum))
        if baseline is not None:
            degradation = float(baseline.get(key, actual)) - actual
            if degradation > max_degradation:
                failures.append(
                    "%s degradation %.6f exceeds %.6f"
                    % (key, degradation, max_degradation)
                )
    if max_latency_ms is not None:
        actual_latency = float(metrics.get("latency_ms", {}).get("p95", 0.0))
        if actual_latency > max_latency_ms:
            failures.append(
                "latency_ms.p95 %.6f exceeds %.6f"
                % (actual_latency, max_latency_ms)
            )
    return {
        "passed": not failures,
        "failures": failures,
        "criteria": {
            "min_recall": min_recall,
            "min_mrr": min_mrr,
            "min_overlap": min_overlap,
            "max_latency_ms": max_latency_ms,
            "max_degradation": max_degradation,
        },
    }


def _matches(record: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    page_ids = {str(value) for value in expected.get("page_ids", [])}
    if page_ids and str(record.get("page_id", "")) in page_ids:
        return True
    title = str(record.get("title", "")).lower()
    if any(str(value).lower() in title for value in expected.get("title_contains", [])):
        return True
    text = str(record.get("text", "")).lower()
    if any(str(value).lower() in text for value in expected.get("answer_contains", [])):
        return True
    return False


def evaluate_retrieval(provider, corpus: Sequence[dict], golden: Mapping, k: int) -> dict:
    documents = provider.embed_documents([str(item.get("text", "")) for item in corpus])
    queries = list(golden.get("queries", []))
    tick = time.perf_counter()
    query_vectors = provider.embed_queries([str(item["query"]) for item in queries])
    query_elapsed = time.perf_counter() - tick
    rankings = []
    relevant = []
    for query, query_vector in zip(queries, query_vectors):
        candidates = [
            (sum(a * b for a, b in zip(query_vector, vector)), str(record["chunk_id"]))
            for record, vector in zip(corpus, documents)
            if not query.get("section") or record.get("section") == query.get("section")
        ]
        rankings.append(
            [chunk_id for _, chunk_id in sorted(candidates, reverse=True)[:k]]
        )
        relevant.append(
            {
                str(record["chunk_id"])
                for record in corpus
                if (not query.get("section") or record.get("section") == query.get("section"))
                and _matches(record, query.get("expected", {}))
            }
        )
    metrics = retrieval_metrics(rankings, relevant, k)
    per_query = query_elapsed / max(len(queries), 1)
    metrics["latency_ms"] = {"p50": per_query * 1000, "p95": per_query * 1000}
    return metrics


def benchmark_provider(provider, corpus: Sequence[dict], batch_size: int) -> dict:
    texts = [str(item.get("text", "")) for item in corpus]
    latencies = []
    dimensions = provider.dimensions
    started = time.perf_counter()
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        tick = time.perf_counter()
        vectors = provider.embed_documents(batch)
        elapsed = time.perf_counter() - tick
        latencies.extend([elapsed / max(len(batch), 1)] * len(batch))
        if vectors:
            dimensions = len(vectors[0])
    duration = time.perf_counter() - started
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, Darwin bytes.
    peak_rss_mb = rss / (1024.0 * (1024.0 if __import__("sys").platform == "darwin" else 1.0))
    return {
        "model_version": provider.model_version,
        "runtime": provider.runtime,
        "documents": len(texts),
        "duration_seconds": duration,
        "throughput_documents_per_second": len(texts) / duration if duration else 0.0,
        "latency_ms": {
            "p50": percentile(latencies, 50) * 1000,
            "p95": percentile(latencies, 95) * 1000,
        },
        "peak_rss_mb": peak_rss_mb,
        "dimensions": dimensions,
    }


def load_corpus(sqlite_path: Path, limit: int) -> list[dict]:
    connection = sqlite3.connect(str(sqlite_path))
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT chunk_id,page_id,section,title,text FROM chunks ORDER BY chunk_id LIMIT ?",
                (limit,),
            )
        ]
    finally:
        connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark E5 runtimes and gate retrieval quality")
    parser.add_argument("--base")
    parser.add_argument("--sqlite")
    parser.add_argument("--provider", action="append", choices=("pytorch", "onnx"))
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--golden", default="fixtures/golden_queries.json")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-recall", type=float, default=0.0)
    parser.add_argument("--min-mrr", type=float, default=0.0)
    parser.add_argument("--min-overlap", type=float, default=0.0)
    parser.add_argument("--max-query-latency-ms", type=float)
    parser.add_argument("--baseline-report")
    parser.add_argument("--max-degradation", type=float, default=0.0)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    corpus = load_corpus(Path(args.sqlite or settings.sqlite_path), args.sample_size)
    if not corpus:
        parser.error("corpus sample is empty")
    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    validate_golden(golden)
    baseline = None
    if args.baseline_report:
        previous = json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
        baseline = previous.get("providers", [{}])[0].get("quality")
    reports = []
    failed = False
    for name in args.provider or [settings.embedding_provider]:
        configured = replace(settings, embedding_provider=name)
        provider = build_provider(configured)
        performance = benchmark_provider(
            provider, corpus, args.batch_size or settings.embedding_batch_size
        )
        quality = evaluate_retrieval(provider, corpus, golden, args.top_k)
        gate = quality_gate(
            quality,
            min_recall=args.min_recall,
            min_mrr=args.min_mrr,
            min_overlap=args.min_overlap,
            max_latency_ms=args.max_query_latency_ms,
            baseline=baseline,
            max_degradation=args.max_degradation,
        )
        failed = failed or not gate["passed"]
        reports.append({**performance, "quality": quality, "quality_gate": gate})
    report = {"schema_version": "1", "providers": reports, "quality_gate_passed": not failed}
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
