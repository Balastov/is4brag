import tempfile
import unittest
from pathlib import Path

from is4brag.embeddings import DeterministicFakeProvider
from is4brag.qdrant import FakeQdrantAdapter, chunk_payload, point_id
from is4brag.store import CanonicalStore
from is4brag.worker import IndexWorker


def make_chunk(chunk_id, page_id, text, digest):
    return {
        "chunk_id": chunk_id,
        "page_id": page_id,
        "section": "S",
        "title": "T",
        "text": text,
        "content_hash": digest,
        "chunker_version": "2",
        "schema_version": "2",
    }


class WorkerQdrantTests(unittest.TestCase):
    def test_worker_reuses_cache_and_applies_deletes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "db.sqlite3")
            first = make_chunk("a", "p1", "identical", "same-hash")
            second = make_chunk("b", "p2", "identical", "same-hash")
            store.replace_page({"page_id": "p1", "section": "S"}, [first], "fake-v1")
            store.replace_page({"page_id": "p2", "section": "S"}, [second], "fake-v1")
            provider = DeterministicFakeProvider()
            vectors = FakeQdrantAdapter()
            worker = IndexWorker(store, provider, vectors, heartbeat_seconds=0.01)
            self.assertEqual(worker.run(max_jobs=2), 2)
            self.assertEqual(provider.texts_embedded, 1)
            self.assertEqual(vectors.count(), 2)

            store.tombstone_page("p1", "fake-v1")
            worker.run(max_jobs=1)
            self.assertNotIn("a", vectors.points)
            store.close()

    def test_fake_adapter_is_idempotent_and_serializes_payload(self):
        adapter = FakeQdrantAdapter(dimensions=2)
        record = make_chunk("stable", "p", "text", "digest")
        adapter.upsert(record, [1, 0], "v")
        adapter.upsert(record, [0, 1], "v")
        self.assertEqual(adapter.count(), 1)
        self.assertEqual(adapter.points["stable"]["payload"], chunk_payload(record, "v"))
        self.assertEqual(point_id("stable"), point_id("stable"))
        adapter.delete("stable")
        adapter.delete("stable")
        self.assertEqual(adapter.count(), 0)

    def test_stale_delete_after_restore_forces_upsert_replay(self):
        class RacingAdapter(FakeQdrantAdapter):
            on_delete = None

            def delete(self, chunk_id):
                super().delete(chunk_id)
                callback, self.on_delete = self.on_delete, None
                if callback:
                    callback()

        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "db.sqlite3")
            record = make_chunk("same", "p", "restored", "digest")
            provider = DeterministicFakeProvider()
            vectors = RacingAdapter()
            store.replace_page({"page_id": "p", "section": "S"}, [record], "fake-v1")
            IndexWorker(store, provider, vectors, worker_id="seed").process_one()
            store.tombstone_page("p", "fake-v1")

            def restore_and_finish_newer_upsert():
                store.replace_page(
                    {"page_id": "p", "section": "S"}, [record], "fake-v1"
                )
                IndexWorker(store, provider, vectors, worker_id="new").process_one()

            vectors.on_delete = restore_and_finish_newer_upsert
            IndexWorker(store, provider, vectors, worker_id="stale").process_one()
            self.assertEqual(store.queue_metrics()["pending"], 1)
            IndexWorker(store, provider, vectors, worker_id="replay").process_one()
            self.assertIn("same", vectors.points)
            store.close()

    def test_stale_upsert_after_delete_forces_delete_replay(self):
        class RacingAdapter(FakeQdrantAdapter):
            on_upsert = None

            def upsert(self, chunk, vector, model_version, provider_runtime=""):
                super().upsert(chunk, vector, model_version, provider_runtime)
                callback, self.on_upsert = self.on_upsert, None
                if callback:
                    callback()

        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "db.sqlite3")
            record = make_chunk("same", "p", "to delete", "digest")
            provider = DeterministicFakeProvider()
            vectors = RacingAdapter()
            store.replace_page({"page_id": "p", "section": "S"}, [record], "fake-v1")

            def delete_and_finish_newer_job():
                store.tombstone_page("p", "fake-v1")
                IndexWorker(store, provider, vectors, worker_id="new").process_one()

            vectors.on_upsert = delete_and_finish_newer_job
            IndexWorker(store, provider, vectors, worker_id="stale").process_one()
            self.assertEqual(store.queue_metrics()["pending"], 1)
            IndexWorker(store, provider, vectors, worker_id="replay").process_one()
            self.assertNotIn("same", vectors.points)
            store.close()


if __name__ == "__main__":
    unittest.main()
