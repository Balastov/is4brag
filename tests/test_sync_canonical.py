import importlib.util
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from is4brag.state import migrate_state
from is4brag.store import CanonicalStore


SCRIPT = Path(__file__).parents[1] / "scripts" / "sync_confluence.py"
SPEC = importlib.util.spec_from_file_location("sync_confluence_test", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class CanonicalSyncTests(unittest.TestCase):
    def test_active_canonical_write_failure_aborts_without_legacy_fallback(self):
        pages = [
            {
                "id": page_id,
                "title": page_id,
                "version": {"number": 1},
                "body": {"storage": {"value": "<p>%s</p>" % ("content " * 100)}},
                "ancestors": [],
            }
            for page_id in ("first", "second")
        ]
        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "db.sqlite3")
            state = migrate_state({}, ["Section"])
            original = store.replace_page
            calls = []

            def fail_second(page, chunks, model_version):
                calls.append(page["page_id"])
                if page["page_id"] == "second":
                    raise RuntimeError("canonical write failed")
                return original(page, chunks, model_version)

            with mock.patch.object(sync, "get_changed_pages", return_value=pages), \
                    mock.patch.object(store, "replace_page", side_effect=fail_second), \
                    mock.patch.object(sync, "reindex_section") as reindex:
                with self.assertRaisesRegex(RuntimeError, "canonical write failed"):
                    sync.sync_section(
                        object(),
                        logging.getLogger("canonical-failure"),
                        temporary,
                        "Section",
                        "root",
                        state,
                        canonical_store=store,
                        model_version="model-v1",
                    )
            self.assertEqual(calls, ["first", "second"])
            reindex.assert_not_called()
            self.assertIsNone(state["sections"]["Section"]["last_sync"])
            store.close()

    def test_canonical_section_failure_does_not_checkpoint_and_fails_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = migrate_state({}, ["Section"])
            store = mock.Mock()
            store.start_sync_run.return_value = 7
            with mock.patch.object(sync, "SECTIONS", {"Section": "root"}), \
                    mock.patch.object(sync, "load_state", return_value=state), \
                    mock.patch.object(sync.Settings, "from_env", return_value=sync.SETTINGS), \
                    mock.patch.object(sync, "CanonicalStore", return_value=store), \
                    mock.patch.object(
                        sync, "sync_section", side_effect=RuntimeError("canonical failed")
                    ), \
                    mock.patch.object(sync, "save_state") as save_state, \
                    mock.patch.object(sync.requests, "Session", return_value=mock.Mock()), \
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "sync_confluence.py",
                            "--base",
                            temporary,
                            "--section",
                            "Section",
                        ],
                    ):
                self.assertEqual(sync.main(), 1)
            save_state.assert_not_called()
            store.finish_sync_run.assert_called_once_with(
                7, "failed", error="canonical failed"
            )
            store.close.assert_called_once()

    def test_inventory_reconcile_repairs_lost_webhook_and_tombstones_deletes(self):
        page = {
            "id": "42",
            "title": "Canonical",
            "version": {"number": 1},
            "body": {"storage": {"value": "<p>%s</p>" % ("content " * 100)}},
            "ancestors": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = CanonicalStore(Path(temporary) / "db.sqlite3")
            state = migrate_state({}, ["Section"])
            logger = logging.getLogger("canonical-sync-test")
            with mock.patch.object(sync, "get_section_inventory", return_value=[page]), \
                    mock.patch.object(sync, "reindex_section") as reindex:
                sync.sync_section(
                    object(),
                    logger,
                    temporary,
                    "Section",
                    "root",
                    state,
                    reconcile=True,
                    canonical_store=store,
                    model_version="model-v1",
                )
            self.assertGreater(store.queue_metrics()["pending"], 0)
            reindex.assert_not_called()

            with mock.patch.object(sync, "get_section_inventory", return_value=[]), \
                    mock.patch.object(sync, "reindex_section") as reindex:
                sync.sync_section(
                    object(),
                    logger,
                    temporary,
                    "Section",
                    "root",
                    state,
                    reconcile=True,
                    canonical_store=store,
                    model_version="model-v1",
                )
            self.assertEqual(store.drift_counts()["chunks"], 0)
            self.assertGreater(
                store.connection.execute(
                    "SELECT count(*) FROM index_jobs WHERE operation='delete'"
                ).fetchone()[0],
                0,
            )
            reindex.assert_not_called()
            store.close()

    def test_migrated_global_state_cannot_tombstone_other_sections(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            store = CanonicalStore(base / "db.sqlite3")
            for page_id, section in (("a", "A"), ("b", "B")):
                store.replace_page(
                    {"page_id": page_id, "section": section},
                    [{
                        "chunk_id": "chunk-" + page_id,
                        "page_id": page_id,
                        "section": section,
                        "text": "content " * 30,
                        "content_hash": "hash-" + page_id,
                        "chunker_version": "3",
                        "schema_version": "2",
                    }],
                    "model-v1",
                )
            legacy = {
                "last_sync": "2026-01-01T00:00:00+0000",
                "page_versions": {"a": 1, "b": 1},
                "inventory": ["a", "b"],
            }
            state = migrate_state(legacy, ["A", "B"])
            page_a = {
                "id": "a",
                "title": "A",
                "version": {"number": 1},
                "body": {"storage": {"value": "<p>%s</p>" % ("A " * 100)}},
                "ancestors": [],
            }
            with mock.patch.object(sync, "get_section_inventory", return_value=[page_a]):
                sync.sync_section(
                    object(),
                    logging.getLogger("migration-safety"),
                    temporary,
                    "A",
                    "root-a",
                    state,
                    reconcile=True,
                    canonical_store=store,
                    model_version="model-v1",
                )
            row = store.connection.execute(
                "SELECT deleted_at FROM pages WHERE page_id='b'"
            ).fetchone()
            self.assertIsNone(row["deleted_at"])
            self.assertTrue(state["sections"]["A"]["ownership_known"])
            store.close()

    def test_root_is_merged_with_changed_descendant(self):
        descendant = {"id": "child", "version": {"when": "2026-01-02T00:00:00+00:00"}}
        root = {"id": "root", "version": {"when": "2026-01-02T00:00:00+00:00"}}

        def api_get(_session, endpoint, _params=None):
            if endpoint == "content/search":
                return {"results": [descendant]}
            self.assertEqual(endpoint, "content/root")
            return root

        with mock.patch.object(sync, "api_get", side_effect=api_get):
            pages = sync.get_changed_pages(
                object(), "root", "2026-01-01T00:00:00+00:00"
            )
        self.assertEqual({page["id"] for page in pages}, {"root", "child"})

    def test_prequery_checkpoint_keeps_during_run_change_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = migrate_state({}, ["A"])
            prequery = "2026-01-01T10:00:00+0000"
            sync.save_state(temporary, state, "A", checkpoint=prequery)
            persisted = json.loads(
                (Path(temporary) / "sync_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["sections"]["A"]["last_sync"], prequery)
            self.assertLessEqual(
                sync._format_cql_date(prequery), "2026-01-01 10:00"
            )

    def test_canonical_drains_legacy_reindex_pending_into_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            section_dir = base / "Section"
            section_dir.mkdir()
            chunk = {
                "chunk_id": "pending-chunk-1",
                "page_id": "pending-page",
                "section": "Section",
                "title": "Pending Page",
                "url": "https://example/pages/pending-page",
                "breadcrumbs": "Section",
                "text": "legacy pending content " * 20,
                "content_hash": "hash-pending",
                "chunker_version": "3",
                "schema_version": "2",
            }
            (section_dir / "chunks_export.jsonl").write_text(
                json.dumps(chunk, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            sync.save_pending_reindex(temporary, {"Section": ["pending-page"]})
            store = CanonicalStore(base / "db.sqlite3")
            state = migrate_state(
                {
                    "sections": {
                        "Section": {
                            "last_sync": "2026-08-08T21:00:00+0000",
                            "page_versions": {"pending-page": 3},
                            "inventory": ["pending-page"],
                            "ownership_known": True,
                        }
                    }
                },
                ["Section"],
            )
            with mock.patch.object(sync, "get_changed_pages", return_value=[]), \
                    mock.patch.object(sync, "reindex_section") as reindex:
                sync.sync_section(
                    object(),
                    logging.getLogger("pending-drain"),
                    temporary,
                    "Section",
                    "root",
                    state,
                    canonical_store=store,
                    model_version="model-v1",
                )
            self.assertGreater(store.queue_metrics()["pending"], 0)
            row = store.connection.execute(
                "SELECT title FROM pages WHERE page_id='pending-page'"
            ).fetchone()
            self.assertEqual(row["title"], "Pending Page")
            self.assertEqual(sync.load_pending_reindex(temporary), {})
            reindex.assert_not_called()
            store.close()


if __name__ == "__main__":
    unittest.main()
