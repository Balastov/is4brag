import json
import tempfile
from pathlib import Path
import unittest

from is4brag.io import atomic_write_json, atomic_write_text
from is4brag.state import load_state, save_state


class StateAndIoTests(unittest.TestCase):
    def test_global_state_migration_discards_unknown_section_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            legacy = {"last_sync": "2026-01-01T00:00:00+0000", "page_versions": {"1": 7}}
            (path / "sync_state.json").write_text(json.dumps(legacy), encoding="utf-8")
            state = load_state(temporary, ["One", "Two"])
            self.assertEqual(state["state_format_version"], 3)
            self.assertEqual(state["sections"]["One"]["page_versions"], {})
            self.assertEqual(state["sections"]["Two"]["inventory"], [])
            self.assertFalse(state["sections"]["One"]["ownership_known"])
            save_state(temporary, state, "One", checkpoint="2025-12-31T23:59:00+0000")
            persisted = json.loads((path / "sync_state.json").read_text())
            self.assertEqual(
                persisted["sections"]["One"]["last_sync"], "2025-12-31T23:59:00+0000"
            )
            self.assertIsNone(persisted["sections"]["Two"]["last_sync"])

    def test_atomic_writes_replace_complete_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "artifact.json"
            atomic_write_text(path, "old")
            atomic_write_json(path, {"value": "new"})
            self.assertEqual(json.loads(path.read_text()), {"value": "new"})
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob(".artifact.json.*.tmp")))
