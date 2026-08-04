import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from is4brag.config import Settings
from is4brag.ingest import (
    IngestEventProcessor,
    WebhookError,
    WebhookService,
)
from is4brag.store import CanonicalStore


def signed(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def page(version=1):
    return {
        "id": "42",
        "title": "Webhook page",
        "version": {"number": version},
        "ancestors": [{"id": "root", "title": "Section"}],
        "body": {"storage": {"value": "<p>%s</p>" % ("canonical content " * 100)}},
    }


class FakeClient:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def fetch_page(self, page_id):
        self.calls.append(page_id)
        return self.value


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.store = CanonicalStore(self.base / "db.sqlite3")
        self.settings = Settings(
            base_path=self.base,
            sandbox_path=self.base,
            model_version="model-v1",
            sections={"Section": "root"},
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_signature_accept_duplicate_and_failure(self):
        service = WebhookService(self.store, "secret")
        body = json.dumps(
            {"event": "page_updated", "page": {"id": "42"}}
        ).encode()
        headers = {
            "X-Hub-Signature-256": signed("secret", body),
            "X-Webhook-Delivery": "delivery-1",
        }
        self.assertEqual(service.handle(body, headers)["status"], "accepted")
        self.assertEqual(service.handle(body, headers)["status"], "duplicate")
        with self.assertRaises(WebhookError) as raised:
            service.handle(body, {**headers, "X-Hub-Signature-256": "sha256=bad"})
        self.assertEqual(raised.exception.code, "invalid_signature")
        metrics = service.metrics.snapshot()
        self.assertEqual(metrics["accepted"], 1)
        self.assertEqual(metrics["duplicate"], 1)
        self.assertEqual(metrics["rejected_signature"], 1)

    def test_size_type_and_source_rejection(self):
        body = b'{"event":"unsupported","page":{"id":"42"}}'
        service = WebhookService(self.store, "secret", max_bytes=8)
        with self.assertRaises(WebhookError) as raised:
            service.handle(body, {"X-Hub-Signature-256": signed("secret", body)})
        self.assertEqual(raised.exception.status, 413)

        service = WebhookService(self.store, "secret")
        headers = {
            "X-Hub-Signature-256": signed("secret", body),
            "X-Webhook-Delivery": "delivery-2",
        }
        with self.assertRaises(WebhookError) as raised:
            service.handle(body, headers)
        self.assertEqual(raised.exception.code, "unsupported_event_type")

        valid = b'{"event":"page_updated","page":{"id":"42"}}'
        service = WebhookService(
            self.store, "secret", allowed_cidrs=("10.0.0.0/8",)
        )
        with self.assertRaises(WebhookError) as raised:
            service.handle(
                valid,
                {
                    "X-Hub-Signature-256": signed("secret", valid),
                    "X-Webhook-Delivery": "delivery-3",
                },
                "192.0.2.1",
            )
        self.assertEqual(raised.exception.code, "source_not_allowed")

    def test_event_queue_lease_retry_and_dead_letter(self):
        self.assertTrue(
            self.store.enqueue_ingest_event("d1", "42", "update", {"page_id": "42"})
        )
        self.assertFalse(
            self.store.enqueue_ingest_event("d1", "42", "update", {"page_id": "42"})
        )
        event = self.store.lease_ingest_events("worker", lease_seconds=30)[0]
        self.assertTrue(
            self.store.heartbeat_ingest_event(event["id"], "worker", lease_seconds=30)
        )
        self.assertEqual(
            self.store.fail_ingest_event(
                event["id"], "worker", "temporary", max_attempts=2, base_backoff=0
            ),
            "pending",
        )
        event = self.store.lease_ingest_events("worker")[0]
        self.assertEqual(
            self.store.fail_ingest_event(
                event["id"], "worker", "permanent", max_attempts=2, base_backoff=0
            ),
            "dead",
        )
        self.assertEqual(self.store.ingest_event_metrics()["dead"], 1)

    def test_two_workers_preserve_per_page_update_delete_order(self):
        self.store.enqueue_ingest_event("page-update", "42", "update")
        self.store.enqueue_ingest_event("page-delete", "42", "delete")
        self.store.enqueue_ingest_event("other-update", "99", "update")

        first = self.store.lease_ingest_events("worker-1", limit=1)
        self.assertEqual(
            [(event["delivery_id"], event["page_id"]) for event in first],
            [("page-update", "42")],
        )
        # A second worker can take another page, but not the later same-page delete.
        other = self.store.lease_ingest_events("worker-2", limit=10)
        self.assertEqual(
            [(event["delivery_id"], event["page_id"]) for event in other],
            [("other-update", "99")],
        )

        processor = IngestEventProcessor(
            self.store, self.settings, FakeClient(page()), worker_id="worker-1"
        )
        processor.process(first[0])
        self.assertTrue(
            self.store.complete_ingest_event(first[0]["id"], "worker-1")
        )
        self.assertTrue(
            self.store.complete_ingest_event(other[0]["id"], "worker-2")
        )

        delete = self.store.lease_ingest_events("worker-2", limit=10)
        self.assertEqual([event["delivery_id"] for event in delete], ["page-delete"])
        delete_processor = IngestEventProcessor(
            self.store, self.settings, FakeClient(page()), worker_id="worker-2"
        )
        delete_processor.process(delete[0])
        self.assertTrue(
            self.store.complete_ingest_event(delete[0]["id"], "worker-2")
        )
        self.assertIsNotNone(
            self.store.connection.execute(
                "SELECT deleted_at FROM pages WHERE page_id='42'"
            ).fetchone()[0]
        )

    def test_update_move_and_delete_use_canonical_and_legacy_pipeline(self):
        client = FakeClient(page())
        processor = IngestEventProcessor(
            self.store, self.settings, client, worker_id="worker"
        )
        self.store.enqueue_ingest_event("d-update", "42", "update")
        self.assertTrue(processor.process_one())
        row = self.store.connection.execute(
            "SELECT section,deleted_at FROM pages WHERE page_id='42'"
        ).fetchone()
        self.assertEqual(tuple(row), ("Section", None))
        export = self.base / "Section" / "chunks_export.jsonl"
        self.assertTrue(export.exists())
        self.assertGreater(len(export.read_text(encoding="utf-8").splitlines()), 0)
        self.assertEqual(client.calls, ["42"])

        self.store.enqueue_ingest_event("d-delete", "42", "delete")
        self.assertTrue(processor.process_one())
        self.assertIsNotNone(
            self.store.connection.execute(
                "SELECT deleted_at FROM pages WHERE page_id='42'"
            ).fetchone()[0]
        )
        self.assertEqual(export.read_text(encoding="utf-8"), "")

    def test_unknown_move_schedules_authoritative_reconcile(self):
        moved = page(2)
        moved["ancestors"] = [{"id": "outside"}]
        processor = IngestEventProcessor(
            self.store, self.settings, FakeClient(moved), worker_id="worker"
        )
        self.store.enqueue_ingest_event("d-move", "42", "move")
        self.assertTrue(processor.process_one())
        pending = json.loads(
            (self.base / "reconcile_pending.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pending["42"]["reason"], "section_membership_unknown")


if __name__ == "__main__":
    unittest.main()
