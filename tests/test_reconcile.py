import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from is4brag.reconcile import append_tombstones, make_tombstones, reconcile_chunks
from is4brag.config import Settings
from is4brag.store import CanonicalStore


SCRIPT = Path(__file__).parents[1] / "scripts" / "reconcile_store.py"
SPEC = importlib.util.spec_from_file_location("reconcile_store_test", SCRIPT)
reconcile_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile_store)


class ReconcileTests(unittest.TestCase):
    def test_reconcile_removes_stale_pages_and_writes_tombstones(self):
        chunks = [
            {"chunk_id": "a", "page_id": "keep"},
            {"chunk_id": "b", "page_id": "stale"},
            {"chunk_id": "c", "page_id": "stale"},
        ]
        kept, stale = reconcile_chunks(chunks, {"keep", "new"}, {"empty-stale"})
        self.assertEqual(kept, [{"chunk_id": "a", "page_id": "keep"}])
        self.assertEqual(stale, {"stale", "empty-stale"})

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tombstones.jsonl"
            append_tombstones(path, make_tombstones("Section", {"stale"}, "2026-01-01T00:00:00Z"))
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(
            records,
            [{
                "section": "Section",
                "page_id": "stale",
                "deleted_at": "2026-01-01T00:00:00Z",
                "reason": "missing_from_confluence_inventory",
                "schema_version": "1",
            }],
        )

    def test_drift_collection_resolves_alias_then_registered_target(self):
        class Probe:
            alias_value = "active-version"

            def __init__(self, *_args, **_kwargs):
                pass

            def alias_target(self, _alias):
                return self.alias_value

        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.from_env(temporary)
            with CanonicalStore(Path(temporary) / "db.sqlite3") as store:
                store.register_index_target(
                    settings.model_version, "runtime", 1024, "registered-version"
                )
                self.assertEqual(
                    reconcile_store.resolve_vector_collection(settings, store, Probe),
                    "active-version",
                )
                Probe.alias_value = None
                self.assertEqual(
                    reconcile_store.resolve_vector_collection(settings, store, Probe),
                    "registered-version",
                )
