import tempfile
import unittest
from pathlib import Path

from is4brag.search import SearchCore, fuse_results
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


if __name__ == "__main__":
    unittest.main()
