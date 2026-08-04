"""Read-only exact and near-duplicate analysis for canonical chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence

from .config import Settings
from .store import CanonicalStore


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def normalized_fingerprint(text: str) -> str:
    return hashlib.sha256(normalized_text(text).encode("utf-8")).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    weights = [0] * bits
    tokens = re.findall(r"\w+", normalized_text(text))
    for token in tokens:
        digest = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        for bit in range(bits):
            weights[bit] += 1 if digest & (1 << bit) else -1
    return sum((1 << bit) for bit, weight in enumerate(weights) if weight >= 0)


def hamming_distance(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def analyze_duplicates(
    records: Sequence[Mapping[str, object]], *, max_hamming: int = 3
) -> dict:
    exact_groups: dict[str, list[Mapping[str, object]]] = {}
    signatures: list[tuple[Mapping[str, object], int]] = []
    fingerprints: dict[str, str] = {}
    for record in records:
        text = str(record.get("text", ""))
        fingerprint = normalized_fingerprint(text)
        chunk_id = str(record.get("chunk_id", ""))
        fingerprints[chunk_id] = fingerprint
        exact_groups.setdefault(fingerprint, []).append(record)
        signatures.append((record, simhash(text)))
    exact = []
    exact_ids = set()
    exact_savings = 0
    for fingerprint, group in exact_groups.items():
        if len(group) < 2:
            continue
        ids = [str(item.get("chunk_id", "")) for item in group]
        exact_ids.update(ids)
        sizes = [len(str(item.get("text", "")).encode("utf-8")) for item in group]
        savings = sum(sizes) - max(sizes)
        exact_savings += savings
        exact.append(
            {
                "fingerprint": fingerprint,
                "chunk_ids": ids,
                "page_ids": sorted({str(item.get("page_id", "")) for item in group}),
                "estimated_savings_bytes": savings,
            }
        )
    near = []
    near_savings = 0
    candidate_pairs = set()
    if max_hamming <= 3:
        buckets: dict[tuple[int, int], list[int]] = {}
        mask = (1 << 16) - 1
        for index, (_, signature) in enumerate(signatures):
            for band in range(4):
                buckets.setdefault((band, (signature >> (band * 16)) & mask), []).append(
                    index
                )
        for indices in buckets.values():
            for position, left_index in enumerate(indices):
                for right_index in indices[position + 1 :]:
                    candidate_pairs.add((left_index, right_index))
    else:
        candidate_pairs = {
            (left, right)
            for left in range(len(signatures))
            for right in range(left + 1, len(signatures))
        }
    for left_index, right_index in sorted(candidate_pairs):
        left, left_hash = signatures[left_index]
        right, right_hash = signatures[right_index]
        left_id = str(left.get("chunk_id", ""))
        right_id = str(right.get("chunk_id", ""))
        if left_id in exact_ids and fingerprints[left_id] == fingerprints[right_id]:
            continue
        distance = hamming_distance(left_hash, right_hash)
        if distance <= max_hamming:
            savings = min(
                len(str(left.get("text", "")).encode("utf-8")),
                len(str(right.get("text", "")).encode("utf-8")),
            )
            near_savings += savings
            near.append(
                {
                    "chunk_ids": [left_id, right_id],
                    "page_ids": [
                        str(left.get("page_id", "")),
                        str(right.get("page_id", "")),
                    ],
                    "hamming_distance": distance,
                    "estimated_savings_bytes": savings,
                }
            )
    return {
        "schema_version": "1",
        "chunks_analyzed": len(records),
        "exact_groups": exact,
        "near_candidates": near,
        "estimated_exact_savings_bytes": exact_savings,
        "estimated_near_savings_bytes": near_savings,
        "near_duplicates_deleted": 0,
        "read_only": True,
    }


def analyze_store(store: CanonicalStore, *, max_hamming: int = 3) -> dict:
    rows = [
        dict(row)
        for row in store.connection.execute(
            "SELECT chunk_id,page_id,section,text FROM chunks ORDER BY chunk_id"
        )
    ]
    return analyze_duplicates(rows, max_hamming=max_hamming)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report exact and near duplicate candidates without deleting data"
    )
    parser.add_argument("--base")
    parser.add_argument("--sqlite")
    parser.add_argument("--max-hamming", type=int, default=3)
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    if args.max_hamming < 0 or args.max_hamming > 64:
        parser.error("--max-hamming must be between 0 and 64")
    settings = Settings.from_env(args.base)
    with CanonicalStore(args.sqlite or settings.sqlite_path) as store:
        report = analyze_store(store, max_hamming=args.max_hamming)
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
