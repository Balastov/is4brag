import tempfile
import unittest
from pathlib import Path

from is4brag.config import Settings

try:
    from fastapi.testclient import TestClient
    from is4brag.api import create_app
except ImportError:
    TestClient = None


class FakeCore:
    def __init__(self):
        self.warmed = False

    def warm(self):
        self.warmed = True

    def reload(self):
        self.warmed = True

    def status(self):
        return {
            "sqlite": True,
            "qdrant": True,
            "model": self.warmed,
            "active_alias": True,
        }

    def search(self, query, *, top_k, sections, use_parents, filters=None):
        return [{
            "section": "A", "title": "T", "url": "", "text": "body",
            "content": "body", "page_id": "p", "score": 1.0,
        }]


@unittest.skipUnless(TestClient is not None, "FastAPI/httpx not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)
        self.core = FakeCore()
        settings = Settings(
            base_path=path,
            sandbox_path=path,
            sqlite_path=path / "api.sqlite3",
            search_admin_token="secret",
            search_timeout=0.2,
            search_concurrency=1,
        )
        app = create_app(settings, core=self.core)
        try:
            self.client_context = TestClient(app)
            self.client = self.client_context.__enter__()
        except TypeError as exc:
            self.skipTest("installed FastAPI/httpx versions are incompatible: %s" % exc)
        self.addCleanup(self.client_context.__exit__, None, None, None)

    def test_health_readiness_search_metrics_and_admin(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        ready = self.client.get("/ready")
        self.assertEqual(ready.status_code, 200)
        response = self.client.post("/search", json={"query": "architecture"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["content"], "body")
        metrics = self.client.get("/metrics").text
        self.assertIn("is4brag_search_requests_total 1", metrics)
        self.assertEqual(self.client.post("/admin/reload").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/admin/reload", headers={"Authorization": "Bearer secret"}
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
