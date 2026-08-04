"""Shared hybrid search over canonical SQLite FTS5 and Qdrant."""

from __future__ import annotations

import math
import re
import threading
from typing import Iterable, Mapping, Optional, Sequence

from .store import FILTER_FIELDS


RRF_K = 60


def _merge_overlapping(parts: Sequence[str], max_overlap: int = 1000) -> str:
    merged = ""
    for part in parts:
        if not merged:
            merged = part
            continue
        maximum = min(len(merged), len(part), max_overlap)
        overlap = 0
        for size in range(maximum, 19, -1):
            if merged[-size:] == part[:size]:
                overlap = size
                break
        merged = merged.rstrip() + "\n" + part[overlap:].lstrip()
    return merged


def _rank_by_section(results: Sequence[Mapping], section_sizes: Mapping[str, int]) -> list[dict]:
    if not results:
        return []
    maximum = max(section_sizes.values(), default=1)
    max_log = math.log(max(maximum, 2))
    grouped: dict[str, list[dict]] = {}
    for result in results:
        grouped.setdefault(str(result.get("section", "")), []).append(dict(result))
    ranked = []
    for section, items in grouped.items():
        weight = math.log(max(section_sizes.get(section, 1), 2)) / max_log
        for rank, item in enumerate(sorted(items, key=lambda value: value["score"], reverse=True)):
            item["section_rank"] = rank / max(weight, 0.01)
            ranked.append(item)
    return ranked


def fuse_results(
    dense: Sequence[Mapping],
    lexical: Sequence[Mapping],
    *,
    section_sizes: Optional[Mapping[str, int]] = None,
    semantic_weight: float = 0.6,
    lexical_weight: float = 0.4,
    limit: int = 10,
) -> list[dict]:
    """Apply the legacy score-augmented, per-section RRF semantics."""
    if semantic_weight < 0 or lexical_weight < 0 or semantic_weight + lexical_weight <= 0:
        raise ValueError("fusion weights must be non-negative and not both zero")
    sizes = section_sizes or {}
    scores: dict[tuple[str, str], dict] = {}
    for source, weight in (
        (_rank_by_section(dense, sizes), semantic_weight),
        (_rank_by_section(lexical, sizes), lexical_weight),
    ):
        for item in source:
            key = (str(item.get("section", "")), str(item.get("chunk_id", item.get("id", ""))))
            contribution = weight * float(item.get("score", 0.0)) / (
                RRF_K + float(item.get("section_rank", 0.0)) + 1.0
            )
            current = scores.get(key)
            if current is None or contribution > current["score"]:
                scores[key] = {**dict(item), "chunk_id": key[1], "score": contribution}
    return sorted(scores.values(), key=lambda item: item["score"], reverse=True)[:limit]


def _fts_query(query: str) -> str:
    # Quoted tokens make punctuation/user operators harmless while retaining OR recall.
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return '""'
    return " OR ".join('"%s"' % term.replace('"', '""') for term in terms)


class SearchCore:
    """One warm provider shared by all searches in a process."""

    def __init__(
        self,
        store,
        provider,
        vector_store,
        *,
        semantic_weight: float = 0.6,
        lexical_weight: float = 0.4,
        candidate_limit: int = 30,
        active_alias: str = "",
    ) -> None:
        self.store = store
        self.provider = provider
        self.vector_store = vector_store
        self.semantic_weight = semantic_weight
        self.lexical_weight = lexical_weight
        self.candidate_limit = candidate_limit
        self.active_alias = active_alias
        self._db_lock = threading.RLock()
        self._warmed = False

    def warm(self) -> None:
        if not self._warmed:
            self.provider.embed_queries(["warmup"])
            self._warmed = True

    def reload(self) -> None:
        """Warm lazily loaded runtime without replacing the shared instance."""
        self._warmed = False
        self.warm()

    def _sections(self, sections: Optional[Sequence[str]]) -> list[Optional[str]]:
        if sections:
            return [str(value) for value in sections]
        with self._db_lock:
            available = [
                str(row[0])
                for row in self.store.connection.execute(
                    "SELECT DISTINCT section FROM chunks ORDER BY section"
                )
                if row[0]
            ]
        return available or [None]

    def _section_sizes(self, sections: Optional[Sequence[str]]) -> dict[str, int]:
        sql = "SELECT section,count(*) AS count FROM chunks"
        params: list[object] = []
        if sections:
            marks = ",".join("?" for _ in sections)
            sql += " WHERE section IN (%s)" % marks
            params.extend(sections)
        sql += " GROUP BY section"
        with self._db_lock:
            return {
                str(row["section"]): int(row["count"])
                for row in self.store.connection.execute(sql, params)
            }

    def _lexical(
        self,
        query: str,
        sections: Optional[Sequence[str]],
        limit: int,
        filters: Optional[Mapping[str, str]],
    ) -> list[dict]:
        found = []
        with self._db_lock:
            for section in self._sections(sections):
                lexical_query = _fts_query(query) if self.store.fts_available else query
                found.extend(
                    self.store.lexical_search(
                        lexical_query, limit, section, filters=filters
                    )
                )
        if not found:
            return []
        # SQLite bm25 is lower-is-better and normally negative.
        strengths = [abs(float(item.get("score", 0.0))) for item in found]
        maximum = max(strengths) or 1.0
        return [
            {**item, "score": abs(float(item.get("score", 0.0))) / maximum}
            for item in found
        ]

    def _dense(
        self,
        query: str,
        sections: Optional[Sequence[str]],
        limit: int,
        filters: Optional[Mapping[str, str]],
    ) -> list[dict]:
        vector = self.provider.embed_queries([query])[0]
        found = []
        for section in self._sections(sections):
            if filters:
                values = self.vector_store.search(
                    vector, limit=limit, section=section, filters=filters
                )
            else:
                # Preserve compatibility with legacy/fake adapters.
                values = self.vector_store.search(
                    vector, limit=limit, section=section
                )
            found.extend(values)
        return [
            {
                **item,
                "chunk_id": str(item.get("chunk_id", item.get("id", ""))),
                "score": max(0.0, float(item.get("score", 0.0))),
            }
            for item in found
        ]

    def _expand(self, results: Iterable[Mapping], use_parents: bool) -> list[dict]:
        unique: dict[str, dict] = {}
        without_page = []
        with self._db_lock:
            for item in results:
                chunk_id = str(item.get("chunk_id", ""))
                chunk = self.store.get_chunk(chunk_id) if chunk_id else None
                merged = {**(chunk or {}), **dict(item)}
                merged["text"] = str(merged.get("text", ""))
                merged["content"] = merged["text"]
                page_id = str(merged.get("page_id", ""))
                if not page_id:
                    without_page.append(merged)
                    continue
                if page_id not in unique or merged["score"] > unique[page_id]["score"]:
                    unique[page_id] = merged
            if use_parents:
                for page_id, item in unique.items():
                    row = self.store.connection.execute(
                        "SELECT page_id,section,title,url,breadcrumbs,parent_text,source_text "
                        "FROM pages "
                        "WHERE page_id=? AND deleted_at IS NULL",
                        (page_id,),
                    ).fetchone()
                    if row:
                        body = str(row["parent_text"] or row["source_text"] or "")
                        if not body:
                            chunks = self.store.connection.execute(
                                "SELECT text FROM chunks WHERE page_id=? "
                                "ORDER BY chunk_index,duplicate_ordinal",
                                (page_id,),
                            ).fetchall()
                            seen = set()
                            parts = []
                            for chunk in chunks:
                                text = str(chunk["text"])
                                text = re.sub(
                                    r"^(?:Путь: .+\n)?Заголовок: .+\n\n", "", text
                                )
                                if text and text not in seen:
                                    seen.add(text)
                                    parts.append(text)
                            body = _merge_overlapping(parts)
                        heading = "Заголовок: %s" % row["title"]
                        if row["breadcrumbs"]:
                            heading = "Путь: %s\n%s" % (row["breadcrumbs"], heading)
                        text = (heading + "\n\n" + body).strip()
                        item.update(dict(row))
                        item.pop("parent_text", None)
                        item.pop("source_text", None)
                        item.update({"text": text, "content": text, "source": "parent"})
        expanded = list(unique.values()) + without_page
        return sorted(expanded, key=lambda item: item["score"], reverse=True)

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        sections: Optional[Sequence[str]] = None,
        use_parents: bool = True,
        filters: Optional[Mapping[str, str]] = None,
    ) -> list[dict]:
        if not query or not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")
        exact_filters = dict(filters or {})
        for key, value in exact_filters.items():
            if key not in FILTER_FIELDS:
                raise ValueError("unsupported filter: %s" % key)
            if not isinstance(value, str) or not value or len(value) > 500:
                raise ValueError("filter %s must be a non-empty string" % key)
        candidate_limit = max(self.candidate_limit, top_k * 3)
        dense = self._dense(query, sections, candidate_limit, exact_filters)
        lexical = self._lexical(query, sections, candidate_limit, exact_filters)
        fused = fuse_results(
            dense,
            lexical,
            section_sizes=self._section_sizes(sections),
            semantic_weight=self.semantic_weight,
            lexical_weight=self.lexical_weight,
            limit=candidate_limit * max(1, len(sections or [None])),
        )
        results = self._expand(fused, use_parents)[:top_k]
        for result in results:
            result["score"] = round(float(result["score"]), 4)
            result["fusion_score"] = result["score"]
        return results

    def status(self) -> dict[str, bool]:
        checks = {"sqlite": False, "qdrant": False, "model": self._warmed, "active_alias": False}
        try:
            with self._db_lock:
                checks["sqlite"] = self.store.connection.execute("SELECT 1").fetchone()[0] == 1
        except Exception:
            pass
        try:
            checks["qdrant"] = bool(self.vector_store.health())
            checks["active_alias"] = (
                not self.active_alias
                or self.vector_store.alias_target(self.active_alias) is not None
            )
        except Exception:
            pass
        return checks
