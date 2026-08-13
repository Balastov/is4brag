#!/usr/bin/env python3
"""Multi-query source pack for acceptance-scenario skill.

Runs several KISU Metro searches (via kisu_metro_search.py / Search API) and
merges hits into buckets for grounded scenario drafting.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


def _search_script() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "kisu-metro" / "scripts" / "kisu_metro_search.py",
        Path("/app/skills/public/kisu-metro/scripts/kisu_metro_search.py"),
        Path("/home/master/deer-flow/skills/public/kisu-metro/scripts/kisu_metro_search.py"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("kisu_metro_search.py not found next to this skill")


def _run_search(
    script: Path,
    query: str,
    *,
    top_k: int,
    section: Optional[str],
) -> list:
    cmd = [sys.executable, str(script), query, "--top-k", str(top_k), "--json"]
    if section:
        cmd.extend(["--section", section])
    env = os.environ.copy()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "search failed (%s): %s" % (proc.returncode, (proc.stderr or proc.stdout)[:500])
        )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return []
    # kisu_metro_search may print logs to stderr; stdout should be JSON array
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # tolerate leading/trailing noise: take last [...] block
        start = stdout.find("[")
        end = stdout.rfind("]")
        if start < 0 or end < start:
            raise
        payload = json.loads(stdout[start : end + 1])
    if not isinstance(payload, list):
        raise RuntimeError("search returned non-list JSON")
    return payload


def _annotate(hits: list[dict[str, Any]], bucket: str, query: str) -> list[dict[str, Any]]:
    out = []
    for hit in hits:
        item = dict(hit)
        item["bucket"] = bucket
        item["query"] = query
        out.append(item)
    return out


def _merge(buckets: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for bucket, hits in buckets.items():
        for hit in hits:
            page_id = str(hit.get("page_id") or "")
            chunk_id = str(hit.get("chunk_id") or "")
            key = (page_id, chunk_id or hit.get("title", ""), bucket)
            prev = merged.get(key)
            if prev is None or float(hit.get("score") or 0) > float(prev.get("score") or 0):
                merged[key] = hit
    return sorted(
        merged.values(),
        key=lambda item: float(item.get("score") or item.get("fusion_score") or 0),
        reverse=True,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True, help="Short topic / subsystem / code family")
    parser.add_argument("--codes", nargs="*", default=[], help="Explicit document codes")
    parser.add_argument("--meeting-query", default="", help="Extra meetings/protocols query")
    parser.add_argument("--process-query", default="", help="Extra BPMN/process query")
    parser.add_argument(
        "--template-query",
        default="шаблон сценария приёмки функционала",
        help="Query for acceptance scenario template pages",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="Print JSON (default)")
    args = parser.parse_args(argv)

    script = _search_script()
    topic = args.topic.strip()
    codes = [code.strip() for code in args.codes if code and code.strip()]
    code_blob = " ".join(codes)

    queries = {
        "decisions": (
            ("%s %s проектное решение ПР" % (topic, code_blob)).strip(),
            "Стадии проекта",
        ),
        "requirements": (
            ("%s %s требование ФТ FTT доработка" % (topic, code_blob)).strip(),
            "Стадии проекта",
        ),
        "process": (
            (args.process_query or ("%s %s BPMN Шторм бизнес-процесс шаг" % (topic, code_blob))).strip(),
            "Стадии проекта",
        ),
        "meetings": (
            (
                args.meeting_query
                or ("%s %s протокол встречи решение совета" % (topic, code_blob))
            ).strip(),
            "Управление проектом",
        ),
        "template": (args.template_query.strip(), None),
    }

    buckets: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for bucket, (query, section) in queries.items():
        if not query:
            buckets[bucket] = []
            continue
        try:
            hits = _run_search(script, query, top_k=args.top_k, section=section)
            buckets[bucket] = _annotate(hits, bucket, query)
        except Exception as exc:  # noqa: BLE001 - surface per-bucket failures to the agent
            buckets[bucket] = []
            errors.append("%s: %s" % (bucket, exc))

    # Exact code probes without section filter (page_id / title boost)
    code_hits: list[dict[str, Any]] = []
    for code in codes:
        try:
            hits = _run_search(script, code, top_k=max(3, args.top_k // 2), section=None)
            code_hits.extend(_annotate(hits, "codes", code))
        except Exception as exc:  # noqa: BLE001
            errors.append("codes/%s: %s" % (code, exc))
    buckets["codes"] = code_hits

    payload = {
        "topic": topic,
        "codes": codes,
        "buckets": {name: hits for name, hits in buckets.items()},
        "merged": _merge(buckets),
        "errors": errors,
        "guidance": [
            "Use only these hits (plus user attachments) as evidence.",
            "Build atomic fact cards with page_id + quote before writing the scenario.",
            "Empty template slots must be marked НЕ НАЙДЕНО В ИСТОЧНИКАХ.",
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if errors and not payload["merged"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
