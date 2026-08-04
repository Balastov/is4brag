import importlib.util
from pathlib import Path
import tempfile
import unittest

from is4brag.duplicates import analyze_duplicates
from is4brag.embeddings import DeterministicFakeProvider
from is4brag.experiment import promotion_allowed, validate_promotion_target
from is4brag.qdrant import FakeQdrantAdapter
from is4brag.store import CanonicalStore
from is4brag.worker import IndexWorker


ROOT = Path(__file__).resolve().parents[1]


class PhaseSevenTests(unittest.TestCase):
    @staticmethod
    def _alias_script():
        path = ROOT / "scripts" / "manage_qdrant_alias.py"
        spec = importlib.util.spec_from_file_location("manage_qdrant_alias_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_near_duplicate_analysis_is_read_only(self):
        records = [
            {"chunk_id": "a", "page_id": "1", "text": "Same, normalized text."},
            {"chunk_id": "b", "page_id": "2", "text": "same normalized text"},
            {"chunk_id": "c", "page_id": "3", "text": "same normalized text extra"},
        ]
        report = analyze_duplicates(records, max_hamming=64)
        self.assertEqual(len(report["exact_groups"]), 1)
        self.assertTrue(report["near_candidates"])
        self.assertEqual(report["near_duplicates_deleted"], 0)
        self.assertTrue(report["read_only"])
        self.assertEqual([item["chunk_id"] for item in records], ["a", "b", "c"])

    def test_experiment_never_promotes_failed_or_missing_gate(self):
        self.assertFalse(promotion_allowed({}))
        self.assertFalse(promotion_allowed({"quality_gate_passed": False}))
        self.assertFalse(promotion_allowed({"quality_gate_passed": True}))
        report = {
            "providers": [{
                "model_version": "model-v1",
                "quality": {"queries_evaluated": 2, "queries_total": 2},
                "quality_gate": {
                    "passed": True,
                    "criteria": {
                        "min_recall": 0.8,
                        "min_mrr": 0.0,
                        "min_overlap": 0.0,
                    },
                },
            }]
        }
        self.assertTrue(promotion_allowed(report, "model-v1"))
        self.assertFalse(promotion_allowed(report, "other-model"))

    def test_promotion_target_requires_settled_matching_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "target.sqlite3")
            record = {
                "chunk_id": "c",
                "page_id": "p",
                "section": "S",
                "text": "content",
                "content_hash": "digest",
                "chunker_version": "3",
                "schema_version": "2",
            }
            store.replace_page({"page_id": "p", "section": "S"}, [record], "model-v1")
            vectors = FakeQdrantAdapter(collection="collection-v1")
            provider = DeterministicFakeProvider(model_version="model-v1")
            store.register_index_target(
                "model-v1", provider.runtime, provider.dimensions, "collection-v1"
            )
            IndexWorker(store, provider, vectors).process_one()
            self.assertEqual(
                validate_promotion_target(
                    store,
                    vectors,
                    model_version="model-v1",
                    chunker_version="3",
                    collection_name="collection-v1",
                ),
                [],
            )
            store.enqueue("delete", "c", "p", "model-v1")
            job = store.lease_jobs("failure")[0]
            store.fail_job(job["id"], "failure", "fatal", max_attempts=1)
            failures = validate_promotion_target(
                store,
                vectors,
                model_version="model-v1",
                chunker_version="3",
                collection_name="collection-v1",
            )
            self.assertTrue(any("not settled" in failure for failure in failures))
            store.close()

    def test_promotion_rejects_equal_count_wrong_point_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "target.sqlite3")
            expected = {
                "chunk_id": "expected",
                "page_id": "p",
                "section": "S",
                "text": "expected content",
                "content_hash": "expected-hash",
                "chunker_version": "3",
                "schema_version": "2",
            }
            store.replace_page({"page_id": "p", "section": "S"}, [expected], "model-v1")
            provider = DeterministicFakeProvider(model_version="model-v1")
            vectors = FakeQdrantAdapter(collection="collection-v1")
            store.register_index_target(
                "model-v1", provider.runtime, provider.dimensions, "collection-v1"
            )
            IndexWorker(store, provider, vectors).process_one()
            wrong = {
                **expected,
                "chunk_id": "wrong",
                "page_id": "other",
                "text": "wrong content",
                "content_hash": "wrong-hash",
            }
            vectors.points.clear()
            vectors.upsert(
                wrong, provider.embed_documents([wrong["text"]])[0], "model-v1"
            )
            failures = validate_promotion_target(
                store,
                vectors,
                model_version="model-v1",
                chunker_version="3",
                collection_name="collection-v1",
            )
            self.assertTrue(any("identities do not match" in item for item in failures))
            store.close()

    def test_promotion_rejects_vector_dimension_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "target.sqlite3")
            record = {
                "chunk_id": "c",
                "page_id": "p",
                "section": "S",
                "text": "content",
                "content_hash": "digest",
                "chunker_version": "3",
                "schema_version": "2",
            }
            store.replace_page({"page_id": "p", "section": "S"}, [record], "model-v1")
            provider = DeterministicFakeProvider(dimensions=7, model_version="model-v1")
            vectors = FakeQdrantAdapter(dimensions=7, collection="collection-v1")
            store.register_index_target(
                "model-v1", provider.runtime, 8, "collection-v1"
            )
            IndexWorker(store, provider, vectors).process_one()
            failures = validate_promotion_target(
                store,
                vectors,
                model_version="model-v1",
                chunker_version="3",
                collection_name="collection-v1",
            )
            self.assertTrue(any("vector dimensions" in item for item in failures))
            store.close()

    def test_alias_utility_has_no_unsafe_promote_operation(self):
        module = self._alias_script()
        with self.assertRaises(SystemExit):
            module.main(
                [
                    "promote",
                    "--target-collection",
                    "target",
                    "--expected-current-collection",
                    "active",
                ]
            )

    def test_alias_rollback_requires_expected_active_collection(self):
        module = self._alias_script()
        adapter = FakeQdrantAdapter(collection="current")
        adapter.collections["previous"] = {}
        adapter.promote_alias("active", "current")
        with self.assertRaisesRegex(RuntimeError, "active alias changed"):
            module.guarded_rollback(
                adapter, "active", "previous", "unexpected-current"
            )
        self.assertEqual(adapter.alias_target("active"), "current")
        self.assertEqual(
            module.guarded_rollback(adapter, "active", "previous", "current"),
            "current",
        )
        self.assertEqual(adapter.alias_target("active"), "previous")

    def test_readme_documents_only_validated_promotion(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("manage_qdrant_alias.py promote", readme)
        self.assertIn("--quality-report reports/e5-v2.json --promote", readme)

    def test_galaktika_cli_imports_without_model_or_data(self):
        path = ROOT / "skills" / "galaktika-erp" / "scripts" / "galaktika_search.py"
        spec = importlib.util.spec_from_file_location("galaktika_smoke", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.search))

    def test_stale_incomplete_index_notice_removed(self):
        skill = (ROOT / "skills" / "kisu-metro" / "SKILL.md").read_text(encoding="utf-8")
        cli = (
            ROOT / "skills" / "kisu-metro" / "scripts" / "kisu_metro_search.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("проиндексирован не полностью", skill + cli)


if __name__ == "__main__":
    unittest.main()
