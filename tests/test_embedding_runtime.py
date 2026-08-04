import hashlib
import tempfile
import unittest
from pathlib import Path

from is4brag.benchmark import quality_gate, retrieval_metrics, validate_golden
from is4brag.embeddings import SentenceTransformerProvider
from is4brag.qdrant import FakeQdrantAdapter
from is4brag.store import CanonicalStore


class RecordingModel:
    def __init__(self):
        self.inputs = []

    def encode(self, texts, **_kwargs):
        self.inputs.append(list(texts))
        return [[1.0, 0.0] for _ in texts]


class EmbeddingRuntimeTests(unittest.TestCase):
    def test_e5_document_and_query_prefixes(self):
        provider = SentenceTransformerProvider("local", dimensions=2, model_version="v2")
        provider._model = RecordingModel()
        provider.embed_documents(["document"])
        provider.embed_queries(["question"])
        self.assertEqual(provider._model.inputs, [["passage: document"], ["query: question"]])
        self.assertEqual(provider.model_version, "v2")
        self.assertEqual(provider.runtime, "sentence-transformers/pytorch")

    def test_metrics_and_quality_gate(self):
        metrics = retrieval_metrics(
            [["a", "x"], ["z", "b"]],
            [{"a", "missing"}, {"b"}],
            2,
        )
        self.assertEqual(metrics["recall_at_k"], 0.75)
        self.assertEqual(metrics["mrr"], 0.75)
        self.assertTrue(quality_gate(metrics, min_recall=0.7, min_mrr=0.7)["passed"])
        failed = quality_gate(
            metrics,
            baseline={"recall_at_k": 0.9, "mrr": 0.9},
            max_degradation=0.1,
        )
        self.assertFalse(failed["passed"])
        self.assertEqual(len(failed["failures"]), 2)

        empty = retrieval_metrics([[]], [set()], 10)
        self.assertFalse(quality_gate(empty)["passed"])

    def test_golden_validation_rejects_placeholders_and_section_only_relevance(self):
        with self.assertRaises(ValueError):
            validate_golden({
                "queries": [{
                    "id": "bad",
                    "query": "q",
                    "expected": {"page_ids": ["<PLACEHOLDER>"]},
                }]
            })
        with self.assertRaises(ValueError):
            validate_golden({
                "queries": [{
                    "id": "bad",
                    "query": "q",
                    "expected": {"section_ids": ["S"]},
                }]
            })

    def test_requeue_reopens_single_versioned_job_and_hashes_exact_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            with CanonicalStore(Path(temporary) / "db.sqlite3") as store:
                record = {
                    "chunk_id": "c1",
                    "page_id": "p1",
                    "section": "S",
                    "text": " exact text ",
                    "content_hash": "stale",
                    "chunker_version": "2",
                    "schema_version": "2",
                }
                store.replace_page({"page_id": "p1", "section": "S"}, [record], "old")
                self.assertEqual(store.requeue_model_version("new"), 1)
                self.assertEqual(store.requeue_model_version("new"), 1)
                rows = store.connection.execute(
                    "SELECT status FROM index_jobs WHERE chunk_id='c1' AND model_version='new'"
                ).fetchall()
                self.assertEqual([row[0] for row in rows], ["pending"])
                self.assertEqual(
                    store.get_chunk("c1")["content_hash"],
                    hashlib.sha256(b" exact text ").hexdigest(),
                )

    def test_fake_alias_promote_and_rollback(self):
        adapter = FakeQdrantAdapter(collection="v1")
        adapter.collections["v2"] = {}
        self.assertIsNone(adapter.promote_alias("active", "v1"))
        self.assertEqual(adapter.promote_alias("active", "v2"), "v1")
        self.assertEqual(adapter.alias_target("active"), "v2")
        self.assertEqual(adapter.rollback_alias("active", "v1"), "v2")
        self.assertEqual(adapter.alias_target("active"), "v1")
        with self.assertRaises(ValueError):
            adapter.promote_alias("active", "missing")


if __name__ == "__main__":
    unittest.main()
