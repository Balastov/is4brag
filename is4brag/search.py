"""Shared hybrid search over canonical SQLite FTS5 and Qdrant."""

from __future__ import annotations

import math
import re
import threading
from typing import Iterable, Mapping, Optional, Sequence

from .store import FILTER_FIELDS


RRF_K = 60

# Confluence page ids in this corpus are 6–12 digits. Bare numeric queries and
# explicit ``pageId …`` mentions must not rely on embeddings or FTS.
_PAGE_ID_BARE = re.compile(r"^\d{6,12}$")
_PAGE_ID_EXPLICIT = re.compile(r"(?i)\bpageId\s*[:=]?\s*(\d{6,12})\b")
# Document codes: UTR_01.01.07.01, ПР_UDO_01.01.01, SND-INT_197_DIP, UBD_01.01-03.
_IDENTIFIER = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9])("
    r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9]*(?:[_.-][A-Za-zА-Яа-яЁё0-9]+)+"
    r")(?![A-Za-zА-Яа-яЁё0-9])"
)


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


def extract_page_ids(query: str) -> list[str]:
    """Return page ids that should be resolved by exact lookup, not embeddings."""
    stripped = query.strip()
    if _PAGE_ID_BARE.fullmatch(stripped):
        return [stripped]
    return list(dict.fromkeys(_PAGE_ID_EXPLICIT.findall(query)))


def extract_identifiers(query: str) -> list[str]:
    """Return document-code tokens worth boosting via title match / phrase FTS.

    Longer codes first so ``ПР_UDO_01.01.01`` outranks a bare ``UDO_01.01``.
    """
    found = list(dict.fromkeys(_IDENTIFIER.findall(query)))
    return sorted(found, key=len, reverse=True)


def _query_terms(query: str) -> list[str]:
    return [
        term.casefold()
        for term in re.findall(r"\w+", query, flags=re.UNICODE)
        if len(term) > 2 and not term.isdigit()
    ]


def _title_match_key(title: str, identifier: str, query: str) -> tuple:
    """Lower tuple sorts better: word overlap, prefix match, early code, short title."""
    title_cf = title.casefold()
    ident_cf = identifier.casefold()
    terms = [term for term in _query_terms(query) if term != ident_cf]
    overlap = sum(1 for term in terms if term in title_cf)
    prefix = 0 if title_cf.startswith(ident_cf) else 1
    position = title_cf.find(ident_cf)
    if position < 0:
        position = 10_000
    return (-overlap, prefix, position, len(title))


def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH query.

    Short pure-numeric fragments (``01``, ``07``) from dotted codes flood OR
    recall, so they are dropped. Dotted/underscored identifiers are kept as
    whole phrases in addition to their meaningful word tokens.
    """
    phrases = extract_identifiers(query)
    terms = [
        term
        for term in re.findall(r"\w+", query, flags=re.UNICODE)
        if not (term.isdigit() and len(term) <= 2)
    ]
    parts: list[str] = []
    seen: set[str] = set()
    for value in phrases + terms:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        parts.append('"%s"' % value.replace('"', '""'))
    if not parts:
        return '""'
    # Identifier-only queries prefer AND so ``UTR_01`` + distinctive parts win
    # over a noisy OR of shared prefixes across the corpus.
    stripped = query.strip()
    if phrases and _IDENTIFIER.fullmatch(stripped):
        significant = [
            '"%s"' % term.replace('"', '""')
            for term in terms
            if not term.isdigit() and len(term) >= 3
        ]
        if len(significant) >= 2:
            return " OR ".join(
                [parts[0], "(" + " AND ".join(significant) + ")"]
            )
        return parts[0]
    return " OR ".join(parts)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _dedupe_by_page(results: Sequence[Mapping], limit: int) -> list[dict]:
    seen: set[str] = set()
    ordered: list[dict] = []
    for item in results:
        page_id = str(item.get("page_id", ""))
        key = page_id or str(item.get("chunk_id", item.get("id", "")))
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(dict(item))
        if len(ordered) >= limit:
            break
    return ordered


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

    def _passes_filters(
        self,
        row: Mapping[str, object],
        sections: Optional[Sequence[str]],
        filters: Optional[Mapping[str, str]],
    ) -> bool:
        if sections and str(row.get("section", "")) not in set(map(str, sections)):
            return False
        for key, value in (filters or {}).items():
            if str(row.get(key, "")) != value:
                return False
        return True

    def _exact_page_matches(
        self,
        query: str,
        sections: Optional[Sequence[str]],
        filters: Optional[Mapping[str, str]],
    ) -> list[dict]:
        page_ids = extract_page_ids(query)
        if not page_ids:
            return []
        found: list[dict] = []
        with self._db_lock:
            for page_id in page_ids:
                row = self.store.connection.execute(
                    "SELECT c.* FROM chunks c "
                    "JOIN pages p ON p.page_id = c.page_id "
                    "WHERE c.page_id = ? AND p.deleted_at IS NULL "
                    "ORDER BY c.chunk_index, c.duplicate_ordinal LIMIT 1",
                    (page_id,),
                ).fetchone()
                if row is None:
                    continue
                item = dict(row)
                if not self._passes_filters(item, sections, filters):
                    continue
                item["score"] = 1.0
                item["match"] = "page_id"
                found.append(item)
        return found

    def _title_identifier_matches(
        self,
        query: str,
        sections: Optional[Sequence[str]],
        filters: Optional[Mapping[str, str]],
        limit: int,
    ) -> list[dict]:
        identifiers = extract_identifiers(query)
        if not identifiers:
            return []
        candidates: list[tuple[tuple, dict]] = []
        seen_pages: set[str] = set()
        with self._db_lock:
            for identifier in identifiers:
                pattern = "%" + _like_escape(identifier) + "%"
                clauses = [
                    "p.deleted_at IS NULL",
                    "(c.title LIKE ? ESCAPE '\\' OR p.title LIKE ? ESCAPE '\\')",
                ]
                params: list[object] = [pattern, pattern]
                if sections:
                    marks = ",".join("?" for _ in sections)
                    clauses.append("c.section IN (%s)" % marks)
                    params.extend(str(value) for value in sections)
                sql = (
                    "SELECT c.*, p.title AS page_title FROM chunks c "
                    "JOIN pages p ON p.page_id = c.page_id "
                    "WHERE %s "
                    "ORDER BY c.chunk_index, c.duplicate_ordinal"
                    % " AND ".join(clauses)
                )
                for row in self.store.connection.execute(sql, params):
                    item = dict(row)
                    page_id = str(item.get("page_id", ""))
                    if page_id in seen_pages or not self._passes_filters(
                        item, None, filters
                    ):
                        continue
                    seen_pages.add(page_id)
                    page_title = str(item.pop("page_title", "") or item.get("title", ""))
                    item["title"] = page_title or str(item.get("title", ""))
                    item["match"] = "title_identifier"
                    key = _title_match_key(item["title"], identifier, query)
                    candidates.append((key, item))
        candidates.sort(key=lambda pair: pair[0])
        found = []
        for index, (_key, item) in enumerate(candidates[:limit]):
            item["score"] = round(0.99 - index * 0.001, 4)
            found.append(item)
        return found

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
        exact_pages = self._exact_page_matches(query, sections, exact_filters)
        title_hits = self._title_identifier_matches(
            query, sections, exact_filters, limit=candidate_limit
        )
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
        # Exact page-id and title-code hits outrank hybrid noise; expand once.
        priority = self._expand(exact_pages + title_hits, use_parents)
        hybrid = self._expand(fused, use_parents)
        results = _dedupe_by_page(priority + hybrid, top_k)
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
