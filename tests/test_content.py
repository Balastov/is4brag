from pathlib import Path
from dataclasses import replace
import tempfile
import unittest

from is4brag.config import Settings
from is4brag.content import content_hash, normalize_html, page_to_chunks, stable_chunk_id


def _page(body: str) -> dict:
    return {
        "id": "123",
        "title": "Test",
        "ancestors": [{"title": "Root"}],
        "body": {"storage": {"value": body}},
    }


class ContentTests(unittest.TestCase):
    def test_tables_and_requirement_metadata_are_preserved(self):
        raw = (Path(__file__).parent / "fixtures" / "confluence_page.html").read_text()
        document = normalize_html(raw)
        self.assertIn("[Таблица 1]", document.text)
        self.assertIn("| заголовок | Код | Описание |", document.text)
        self.assertIn("| строка 2 | REQ-42 | Быстрый ответ |", document.text)
        self.assertEqual(
            document.requirements,
            [{"data-requirement-id": "REQ-42", "data-status": "approved"}],
        )

    def test_chunk_ids_are_stable_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.from_env(temporary)
            body = "<p>%s</p>" % ("stable prose " * 80)
            first = page_to_chunks(_page(body), "Section", settings)
            second = page_to_chunks(_page(body), "Section", settings)
        self.assertEqual(
            [chunk["chunk_id"] for chunk in first],
            [chunk["chunk_id"] for chunk in second],
        )
        self.assertTrue(all(chunk["content_hash"] for chunk in first))
        self.assertTrue(all(chunk["chunker_version"] == "3" for chunk in first))
        self.assertTrue(all(chunk["schema_version"] == "2" for chunk in first))

    def test_duplicate_chunks_get_distinct_deterministic_ordinals(self):
        digest = content_hash("same")
        first = stable_chunk_id("Section", "123", digest, 0)
        duplicate = stable_chunk_id("Section", "123", digest, 1)
        self.assertNotEqual(first, duplicate)
        self.assertEqual(first, stable_chunk_id("Section", "123", digest, 0))

        with tempfile.TemporaryDirectory() as temporary:
            settings = replace(
                Settings.from_env(temporary),
                chunk_size=40,
                chunk_overlap=0,
                min_chunk_len=10,
            )
            chunks = page_to_chunks(_page("<p>%s</p>" % ("A" * 120)), "Section", settings)
        self.assertEqual([chunk["duplicate_ordinal"] for chunk in chunks], [0, 1, 2])
        self.assertEqual(len({chunk["content_hash"] for chunk in chunks}), 1)
        self.assertEqual(len({chunk["chunk_id"] for chunk in chunks}), 3)

    def test_content_aware_headings_tables_requirements_and_versions(self):
        body = """
        <h1>Architecture</h1><p>Intro text long enough for a useful chunk.</p>
        <table><tr><th>ID</th><th>Text</th></tr>
        <tr><td>1</td><td>First complete row</td></tr>
        <tr><td>2</td><td>Second complete row</td></tr></table>
        <p data-requirement-id="REQ-7" data-parent-ref="REQ-1"
           data-child-ref="REQ-8">REQ-7 must remain traceable with metadata.</p>
        """
        with tempfile.TemporaryDirectory() as temporary:
            settings = replace(
                Settings.from_env(temporary),
                chunk_size=90,
                chunk_overlap=10,
                min_chunk_len=10,
            )
            chunks = page_to_chunks(_page(body), "Section", settings)
            legacy = page_to_chunks(
                _page(body),
                "Section",
                replace(settings, chunker_version="legacy-v1", chunk_strategy="legacy"),
            )
        self.assertTrue(any(item["content_type"] == "table" for item in chunks))
        self.assertTrue(
            all(
                "| строка" not in item["text"] or "| заголовок |" in item["text"]
                for item in chunks
            )
        )
        requirement = next(item for item in chunks if item["content_type"] == "requirement")
        self.assertEqual(requirement["parent_references"], ["REQ-1"])
        self.assertEqual(requirement["child_references"], ["REQ-8"])
        self.assertTrue(any("# Architecture" in item["text"] for item in chunks))
        self.assertNotEqual(
            [item["chunk_id"] for item in chunks],
            [item["chunk_id"] for item in legacy],
        )
