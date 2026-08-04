import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock
from urllib.error import URLError

from is4brag.client import SearchClient, search_with_fallback
from is4brag.shadow import compare_backends


ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


class SearchClientAndShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "skills" / "kisu-metro" / "scripts" / "kisu_metro_search.py"
        spec = importlib.util.spec_from_file_location("kisu_search_fallback_test", path)
        cls.skill = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.skill)

    def test_client_decodes_results(self):
        client = SearchClient(
            "http://search",
            opener=lambda request, timeout: Response(b'{"results":[{"page_id":"p1"}]}'),
        )
        self.assertEqual(client.search("query")[0]["page_id"], "p1")

    def test_client_falls_back_on_transport_error(self):
        def fail(_request, timeout):
            raise URLError("offline")

        client = SearchClient("http://search", opener=fail)
        result = search_with_fallback(
            client, lambda: [{"page_id": "legacy"}], query="query"
        )
        self.assertEqual(result[0]["page_id"], "legacy")

    def test_skill_api_failure_is_fail_closed_by_default(self):
        with mock.patch.dict(
            os.environ,
            {"SEARCH_API_URL": "http://search"},
            clear=True,
        ), mock.patch.object(
            self.skill, "_api_search", side_effect=URLError("offline")
        ), mock.patch.object(
            self.skill, "legacy_search"
        ) as legacy:
            with self.assertRaises(URLError):
                self.skill.search("query")
        legacy.assert_not_called()

    def test_skill_legacy_fallback_requires_explicit_opt_in(self):
        expected = [{"page_id": "legacy"}]
        with mock.patch.dict(
            os.environ,
            {
                "SEARCH_API_URL": "http://search",
                "SEARCH_API_LEGACY_FALLBACK": "1",
            },
            clear=True,
        ), mock.patch.object(
            self.skill, "_api_search", side_effect=URLError("offline")
        ), mock.patch.object(
            self.skill, "legacy_search", return_value=expected
        ) as legacy:
            self.assertEqual(self.skill.search("query"), expected)
        legacy.assert_called_once()

    def test_skill_without_api_url_preserves_legacy_only_mode(self):
        expected = [{"page_id": "legacy"}]
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            self.skill, "legacy_search", return_value=expected
        ) as legacy:
            self.assertEqual(self.skill.search("query"), expected)
        legacy.assert_called_once()

    def test_shadow_report_and_quality_gate_use_injected_backends(self):
        golden = {
            "queries": [
                {
                    "id": "q",
                    "query": "query",
                    "expected": {"page_ids": ["p1"]},
                }
            ]
        }

        def legacy(query, top_k, sections):
            return [{"page_id": "p1"}, {"page_id": "p2"}]

        def new(query, top_k, sections):
            return [{"page_id": "p2"}, {"page_id": "p1"}]

        report = compare_backends(legacy, new, golden, top_k=2, min_overlap=1.0)
        self.assertTrue(report["quality_gate"]["passed"])
        self.assertEqual(report["summary"]["overlap_at_k"], 1.0)
        self.assertEqual(report["queries"][0]["rank_differences"]["p1"], 1)


if __name__ == "__main__":
    unittest.main()
