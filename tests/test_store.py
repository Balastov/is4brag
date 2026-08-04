import tempfile
import unittest
from pathlib import Path

from is4brag.store import CanonicalStore


def chunk(chunk_id="c1", text="searchable architecture", digest="hash"):
    return {
        "chunk_id": chunk_id,
        "page_id": "p1",
        "section": "Architecture",
        "title": "Design",
        "text": text,
        "content_hash": digest,
        "chunker_version": "2",
        "schema_version": "2",
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self.temporary.name) / "canonical.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_migration_wal_replace_fts_and_tombstone(self):
        self.assertEqual(
            self.store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal"
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT max(version) FROM schema_migrations"
            ).fetchone()[0],
            5,
        )
        page = {"page_id": "p1", "section": "Architecture", "schema_version": "2"}
        self.store.replace_page(page, [chunk()], "model-v1")
        self.assertEqual(self.store.drift_counts()["chunks"], 1)
        self.assertEqual(self.store.lexical_search("architecture")[0]["chunk_id"], "c1")

        self.store.replace_page(page, [chunk("c2", "replacement")], "model-v1")
        self.assertIsNone(self.store.get_chunk("c1"))
        operations = self.store.connection.execute(
            "SELECT operation,chunk_id FROM index_jobs ORDER BY id"
        ).fetchall()
        self.assertIn(("delete", "c1"), [tuple(row) for row in operations])
        self.assertEqual(self.store.tombstone_page("p1", "model-v1"), 1)
        self.assertEqual(self.store.drift_counts()["chunks"], 0)

    def test_queue_lease_retry_dead_letter_and_deduplication(self):
        self.store.enqueue("upsert", "c1", "p1", "model-v1")
        self.store.enqueue("upsert", "c1", "p1", "model-v1")
        self.assertEqual(self.store.queue_metrics()["pending"], 1)
        job = self.store.lease_jobs("worker", lease_seconds=30)[0]
        self.assertTrue(self.store.heartbeat(job["id"], "worker", 30))
        self.assertEqual(
            self.store.fail_job(
                job["id"], "worker", "transient", max_attempts=2, base_backoff=0
            ),
            "pending",
        )
        job = self.store.lease_jobs("worker")[0]
        self.assertEqual(
            self.store.fail_job(
                job["id"], "worker", "permanent", max_attempts=2, base_backoff=0
            ),
            "dead",
        )
        self.assertEqual(self.store.queue_metrics()["dead"], 1)

    def test_embedding_cache_uses_hash_and_model(self):
        self.store.put_embedding("same", "v1", [1, 2])
        self.assertEqual(self.store.get_embedding("same", "v1"), [1.0, 2.0])
        self.assertIsNone(self.store.get_embedding("same", "v2"))

    def test_exact_metadata_filters(self):
        page = {
            "page_id": "p1",
            "section": "Architecture",
            "title": "Design",
            "breadcrumbs": "Root > Design",
            "schema_version": "2",
        }
        record = chunk()
        record["content_type"] = "requirement"
        self.store.replace_page(page, [record], "model-v1")
        self.assertEqual(
            self.store.lexical_search(
                "architecture",
                filters={"page_id": "p1", "content_type": "requirement"},
            )[0]["chunk_id"],
            "c1",
        )
        self.assertEqual(
            self.store.lexical_search("architecture", filters={"title": "Wrong"}), []
        )
        with self.assertRaises(ValueError):
            self.store.lexical_search("architecture", filters={"unsafe": "x"})

    def test_partially_recorded_migration_recovers_idempotently(self):
        path = self.store.path
        self.store.connection.execute("DELETE FROM schema_migrations WHERE version=1")
        self.store.close()
        self.store = CanonicalStore(path)
        versions = [
            row[0]
            for row in self.store.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        self.assertEqual(versions, [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
