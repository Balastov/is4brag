import json
import tempfile
import unittest
from pathlib import Path

from is4brag.importer import import_section
from is4brag.store import CanonicalStore


class ImporterTests(unittest.TestCase):
    def test_importer_normalizes_legacy_metadata_and_queues_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            section = base / "Legacy"
            section.mkdir()
            legacy = {
                "chunk_id": 0,
                "page_id": "42",
                "title": "Old",
                "text": "legacy content",
            }
            (section / "chunks_export.jsonl").write_text(
                json.dumps(legacy) + "\n", encoding="utf-8"
            )
            store = CanonicalStore(base / "db.sqlite3")
            report = import_section(store, section, "model", expected_dimensions=2)
            row = store.connection.execute("SELECT * FROM chunks").fetchone()
            self.assertEqual(row["section"], "Legacy")
            self.assertEqual(row["schema_version"], "legacy")
            self.assertEqual(report["chunks"], 1)
            self.assertEqual(store.queue_metrics()["pending"], 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
