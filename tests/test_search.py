import tempfile
import unittest
from pathlib import Path

from is4brag.search import (
    SearchCore,
    _fts_query,
    extract_identifiers,
    extract_page_ids,
    fuse_results,
)
from is4brag.store import CanonicalStore


class Provider:
    model_version = "fake"
    runtime = "fake"
    dimensions = 2

    def __init__(self):
        self.calls = 0

    def embed_queries(self, texts):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


class Vectors:
    def __init__(self, results):
        self.results = results

    def search(self, vector, limit=10, section=None, filters=None):
        return [
            item for item in self.results
            if (not section or item["section"] == section)
            and all(str(item.get(key, "")) == value for key, value in (filters or {}).items())
        ][:limit]

    def health(self):
        return True

    def alias_target(self, alias):
        return "collection" if alias == "active" else None


def chunk(chunk_id, page_id, section, text, index=0):
    return {
        "chunk_id": chunk_id,
        "page_id": page_id,
        "section": section,
        "title": "Title " + page_id,
        "url": "https://example/" + page_id,
        "text": text,
        "chunk_index": index,
        "content_hash": chunk_id,
        "chunker_version": "2",
        "schema_version": "2",
    }


class SearchCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self.temporary.name) / "store.sqlite3")
        self.store.replace_page(
            {
                "page_id": "p1",
                "section": "A",
                "title": "Parent",
                "schema_version": "2",
                "parent_text": "canonical architecture document",
            },
            [
                chunk("c1", "p1", "A", "architecture first", 0),
                chunk("c2", "p1", "A", "architecture second", 1),
            ],
            "fake",
        )
        self.store.replace_page(
            {"page_id": "p2", "section": "B", "title": "Other", "schema_version": "2"},
            [chunk("c3", "p2", "B", "architecture other")],
            "fake",
        )
        self.provider = Provider()
        self.vectors = Vectors(
            [
                {"chunk_id": "c1", "section": "A", "score": 0.9},
                {"chunk_id": "c2", "section": "A", "score": 0.8},
                {"chunk_id": "c3", "section": "B", "score": 0.7},
            ]
        )
        self.core = SearchCore(
            self.store, self.provider, self.vectors, active_alias="active"
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_fusion_uses_legacy_max_contribution(self):
        fused = fuse_results(
            [{"chunk_id": "x", "section": "A", "score": 1.0}],
            [{"chunk_id": "x", "section": "A", "score": 1.0}],
            limit=1,
        )
        self.assertAlmostEqual(fused[0]["score"], 0.6 / 61)

    def test_section_filter_dedupe_and_parent_expansion(self):
        results = self.core.search(
            "architecture", top_k=5, sections=["A"], use_parents=True
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["page_id"], "p1")
        self.assertEqual(results[0]["source"], "parent")
        self.assertIn("canonical architecture document", results[0]["content"])
        self.assertIn("canonical architecture document", results[0]["text"])
        self.assertNotIn("architecture second", results[0]["text"])
        self.assertEqual(results[0]["section"], "A")

    def test_chunk_mode_preserves_deerflow_fields_and_dedupes_pages(self):
        result = self.core.search("architecture", top_k=1, use_parents=False)[0]
        for field in ("section", "title", "url", "text", "content", "page_id", "score"):
            self.assertIn(field, result)

    def test_warm_and_readiness(self):
        self.assertFalse(self.core.status()["model"])
        self.core.warm()
        self.assertTrue(all(self.core.status().values()))
        self.assertEqual(self.provider.calls, 1)

    def test_bare_page_id_lookup_outranks_hybrid(self):
        self.store.replace_page(
            {
                "page_id": "12366112",
                "section": "A",
                "title": "UF cashflow BP",
                "schema_version": "2",
                "parent_text": "planning cash",
            },
            [chunk("c-pid", "12366112", "A", "unrelated body about widgets")],
            "fake",
        )
        results = self.core.search("12366112", top_k=3, use_parents=False)
        self.assertEqual(results[0]["page_id"], "12366112")
        self.assertEqual(results[0].get("match"), "page_id")

    def test_explicit_page_id_in_mixed_query(self):
        self.store.replace_page(
            {
                "page_id": "88428367",
                "section": "A",
                "title": "UPO PKI scenario",
                "schema_version": "2",
            },
            [chunk("c-upo", "88428367", "A", "annual production planning")],
            "fake",
        )
        results = self.core.search(
            "PKI UPO_01.02-02 pageId 88428367", top_k=3, use_parents=False
        )
        self.assertEqual(results[0]["page_id"], "88428367")

    def test_title_identifier_boost_for_document_codes(self):
        self.store.replace_page(
            {
                "page_id": "12366437",
                "section": "A",
                "title": "Бизнес-процесс UTR_01.01.07.01 Формирование инвестиционной программы",
                "schema_version": "2",
            },
            [
                chunk(
                    "c-utr",
                    "12366437",
                    "A",
                    "Заголовок: Бизнес-процесс UTR_01.01.07.01 Формирование",
                )
            ],
            "fake",
        )
        # Dense vectors only know about architecture chunks; code must win via title.
        results = self.core.search("UTR_01.01.07.01", top_k=3, use_parents=False)
        self.assertEqual(results[0]["page_id"], "12366437")
        self.assertEqual(results[0].get("match"), "title_identifier")


class QueryParsingTests(unittest.TestCase):
    def test_extract_page_ids(self):
        self.assertEqual(extract_page_ids("12366112"), ["12366112"])
        self.assertEqual(
            extract_page_ids("PKI pageId 88428367 more"), ["88428367"]
        )
        self.assertEqual(extract_page_ids("UTR_01.01.07.01"), [])

    def test_extract_identifiers(self):
        self.assertEqual(extract_identifiers("UTR_01.01.07.01"), ["UTR_01.01.07.01"])
        self.assertIn("SND-INT_197_DIP", extract_identifiers("SND-INT_197_DIP Directum"))
        self.assertEqual(
            extract_identifiers("ПР_UDO_01.01.01 Управление договорной"),
            ["ПР_UDO_01.01.01"],
        )
        self.assertEqual(extract_identifiers("обычный текст без кода"), [])

    def test_cyrillic_prefix_title_ranks_primary_document(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = CanonicalStore(Path(temporary.name) / "store.sqlite3")
        self.addCleanup(store.close)
        store.replace_page(
            {
                "page_id": "12365330",
                "section": "A",
                "title": "ПР_UDO_01.01.01 Управление договорной документацией",
                "schema_version": "2",
            },
            [chunk("c-pr", "12365330", "A", "contract docs")],
            "fake",
        )
        store.replace_page(
            {
                "page_id": "28361577",
                "section": "A",
                "title": 'Этап 6_РЗ_ПР_UDO_01.01.01 "Управление договорной документацией"',
                "schema_version": "2",
            },
            [chunk("c-rz", "28361577", "A", "review remarks")],
            "fake",
        )
        core = SearchCore(store, Provider(), Vectors([]))
        results = core.search(
            "ПР_UDO_01.01.01 Управление договорной документацией роли",
            top_k=3,
            use_parents=False,
        )
        self.assertEqual(results[0]["page_id"], "12365330")

    def test_fts_query_drops_short_numeric_noise(self):
        query = _fts_query("UTR_01.01.07.01")
        self.assertIn('"UTR_01.01.07.01"', query)
        self.assertNotIn('"01"', query)
        self.assertNotIn('"07"', query)


if __name__ == "__main__":
    unittest.main()
