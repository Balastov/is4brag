"""Verified Confluence webhook ingestion with durable SQLite delivery."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import signal
import socket
import threading
import time
from typing import Mapping, Optional

from .config import Settings
from .content import normalize_html, page_breadcrumbs, page_to_chunks
from .io import FileLock, atomic_write_json, atomic_write_jsonl
from .state import load_state, save_state, section_state
from .store import CanonicalStore


EVENT_ALIASES = {
    "page_created": "create",
    "page_created_event": "create",
    "created": "create",
    "page_updated": "update",
    "page_edited": "update",
    "updated": "update",
    "page_moved": "move",
    "moved": "move",
    "page_archived": "archive",
    "page_trashed": "delete",
    "page_removed": "delete",
    "page_deleted": "delete",
    "deleted": "delete",
    "page_restored": "update",
}
FETCH_EVENTS = {"create", "update", "move"}
DELETE_EVENTS = {"archive", "delete"}


class WebhookError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


class WebhookMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.values = {
            "accepted": 0,
            "duplicate": 0,
            "rejected_signature": 0,
            "rejected_type": 0,
            "rejected_size": 0,
            "rejected_source": 0,
            "rejected_payload": 0,
        }

    def increment(self, name: str) -> None:
        with self._lock:
            self.values[name] = self.values.get(name, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.values)


def _header(headers: Mapping[str, str], *names: str) -> str:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return ""


def _allowed_source(source: Optional[str], allowed_cidrs: tuple[str, ...]) -> bool:
    if not allowed_cidrs:
        return True
    if not source:
        return False
    try:
        address = ipaddress.ip_address(source)
        return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs)
    except ValueError:
        return False


def _event_fields(payload: Mapping[str, object], headers: Mapping[str, str]) -> tuple[str, str, str]:
    raw_type = (
        _header(headers, "X-Event-Type", "X-Event-Key")
        or payload.get("eventType")
        or payload.get("event")
        or payload.get("type")
        or ""
    )
    event_type = EVENT_ALIASES.get(str(raw_type).strip().lower(), "")
    page = payload.get("page") or payload.get("content") or {}
    if not isinstance(page, Mapping):
        page = {}
    page_id = str(
        page.get("id")
        or payload.get("pageId")
        or payload.get("contentId")
        or payload.get("id")
        or ""
    )
    delivery_id = str(
        _header(
            headers,
            "X-Atlassian-Webhook-Delivery",
            "X-Atlassian-Webhook-Identifier",
            "X-Webhook-Delivery",
            "X-Request-ID",
        )
        or payload.get("deliveryId")
        or payload.get("webhookEventId")
        or ""
    )
    return event_type, page_id, delivery_id


class WebhookService:
    """Framework-independent handler. Validation does no remote or indexing work."""

    def __init__(
        self,
        store: CanonicalStore,
        secret: str,
        *,
        max_bytes: int = 256 * 1024,
        allowed_cidrs: tuple[str, ...] = (),
        metrics: Optional[WebhookMetrics] = None,
    ) -> None:
        self.store = store
        self.secret = secret
        self.max_bytes = max(1, int(max_bytes))
        self.allowed_cidrs = tuple(allowed_cidrs)
        self.metrics = metrics or WebhookMetrics()

    def handle(
        self, body: bytes, headers: Mapping[str, str], source: Optional[str] = None
    ) -> dict:
        if len(body) > self.max_bytes:
            self.metrics.increment("rejected_size")
            raise WebhookError("payload_too_large", 413)
        if not self.secret:
            self.metrics.increment("rejected_signature")
            raise WebhookError("webhook_not_configured", 503)
        if not _allowed_source(source, self.allowed_cidrs):
            self.metrics.increment("rejected_source")
            raise WebhookError("source_not_allowed", 403)
        supplied = _header(
            headers, "X-Hub-Signature-256", "X-Hub-Signature", "X-Webhook-Signature"
        )
        if supplied.lower().startswith("sha256="):
            supplied = supplied[7:]
        expected = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not supplied or not hmac.compare_digest(supplied.lower(), expected):
            self.metrics.increment("rejected_signature")
            raise WebhookError("invalid_signature", 401)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.metrics.increment("rejected_payload")
            raise WebhookError("invalid_payload", 400)
        if not isinstance(payload, Mapping):
            self.metrics.increment("rejected_payload")
            raise WebhookError("invalid_payload", 400)
        event_type, page_id, delivery_id = _event_fields(payload, headers)
        if not event_type:
            self.metrics.increment("rejected_type")
            raise WebhookError("unsupported_event_type", 422)
        if not page_id or not delivery_id or len(page_id) > 128 or len(delivery_id) > 255:
            self.metrics.increment("rejected_payload")
            raise WebhookError("missing_event_identity", 422)
        minimal = {"delivery_id": delivery_id, "page_id": page_id, "event_type": event_type}
        accepted = self.store.enqueue_ingest_event(
            delivery_id, page_id, event_type, minimal
        )
        self.metrics.increment("accepted" if accepted else "duplicate")
        return {"status": "accepted" if accepted else "duplicate", "delivery_id": delivery_id}


class ConfluenceClient:
    def __init__(self, settings: Settings, session=None) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.url = settings.confluence_url
        self.pat = settings.confluence_pat
        self.timeout = settings.request_timeout

    def fetch_page(self, page_id: str) -> dict:
        response = self.session.get(
            "%s/rest/api/content/%s" % (self.url, page_id),
            params={"expand": "body.storage,version,ancestors,space"},
            headers={"Authorization": "Bearer %s" % self.pat},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


class IngestEventProcessor:
    def __init__(
        self,
        store: CanonicalStore,
        settings: Settings,
        client,
        *,
        worker_id: Optional[str] = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
        base_backoff: float = 5,
    ) -> None:
        self.store = store
        self.settings = settings
        self.client = client
        self.worker_id = worker_id or "%s:%s" % (socket.gethostname(), os.getpid())
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.stop_event = threading.Event()

    def stop(self, *_args: object) -> None:
        self.stop_event.set()

    def _known_section(self, page_id: str) -> Optional[str]:
        row = self.store.connection.execute(
            "SELECT section FROM pages WHERE page_id=?", (str(page_id),)
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def _page_section(self, page: Mapping[str, object]) -> Optional[str]:
        ids = {str(page.get("id", ""))}
        ancestors = page.get("ancestors", [])
        if isinstance(ancestors, list):
            ids.update(
                str(item.get("id", ""))
                for item in ancestors
                if isinstance(item, Mapping)
            )
        for section, root_id in self.settings.sections.items():
            if str(root_id) in ids:
                return section
        return None

    def _legacy_records(self, section: str) -> list[dict]:
        path = self.settings.base_path / section / "chunks_export.jsonl"
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _write_legacy_page(
        self, page_id: str, section: str, chunks: list[dict], old_section: Optional[str]
    ) -> None:
        affected = {section}
        if old_section:
            affected.add(old_section)
        for name in affected:
            records = [
                item
                for item in self._legacy_records(name)
                if str(item.get("page_id", "")) != page_id
            ]
            if name == section:
                records.extend(chunks)
            atomic_write_jsonl(
                self.settings.base_path / name / "chunks_export.jsonl", records
            )

    def _delete_legacy_page(self, page_id: str, section: Optional[str]) -> None:
        if not section:
            return
        records = [
            item
            for item in self._legacy_records(section)
            if str(item.get("page_id", "")) != page_id
        ]
        atomic_write_jsonl(
            self.settings.base_path / section / "chunks_export.jsonl", records
        )

    def _update_state(
        self,
        page_id: str,
        section: Optional[str],
        version: Optional[int],
        old_section: Optional[str] = None,
    ) -> None:
        state = load_state(str(self.settings.base_path), self.settings.sections)
        if old_section and old_section != section:
            old = section_state(state, old_section)
            old["page_versions"].pop(page_id, None)
            old["inventory"] = [item for item in old["inventory"] if str(item) != page_id]
        if section:
            current = section_state(state, section)
            if version is None:
                current["page_versions"].pop(page_id, None)
                current["inventory"] = [
                    item for item in current["inventory"] if str(item) != page_id
                ]
            else:
                current["page_versions"][page_id] = int(version)
                current["inventory"] = sorted(
                    set(map(str, current.get("inventory", []))) | {page_id}
                )
        save_state(str(self.settings.base_path), state)

    def _schedule_reconcile(self, page_id: str, reason: str) -> None:
        path = self.settings.base_path / "reconcile_pending.json"
        current = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
        current[str(page_id)] = {"reason": reason, "scheduled_at": time.time()}
        atomic_write_json(path, current)

    def process(self, event: Mapping[str, object]) -> None:
        page_id = str(event["page_id"])
        event_type = str(event["event_type"])
        old_section = self._known_section(page_id)
        if event_type in DELETE_EVENTS:
            with FileLock(self.settings.base_path / ".sync_confluence.lock", timeout=30):
                self.store.tombstone_page(page_id, self.settings.model_version)
                self._delete_legacy_page(page_id, old_section)
                self._update_state(page_id, old_section, None)
            if not old_section:
                self._schedule_reconcile(page_id, "delete_section_unknown")
            return
        if event_type not in FETCH_EVENTS:
            raise ValueError("unsupported queued event type")
        page = self.client.fetch_page(page_id)
        section = self._page_section(page)
        if not section:
            self._schedule_reconcile(page_id, "section_membership_unknown")
            return
        chunks = page_to_chunks(page, section, self.settings)
        source_text = normalize_html(
            page.get("body", {}).get("storage", {}).get("value", "")
        ).text
        version = int(page.get("version", {}).get("number", 0))
        with FileLock(self.settings.base_path / ".sync_confluence.lock", timeout=30):
            current = self.store.connection.execute(
                "SELECT confluence_version FROM pages WHERE page_id=?", (page_id,)
            ).fetchone()
            if current and int(current[0]) > version:
                return
            self.store.replace_page(
                {
                    "page_id": page_id,
                    "section": section,
                    "title": page.get("title", ""),
                    "url": "%s/pages/%s" % (self.settings.confluence_url, page_id),
                    "breadcrumbs": page_breadcrumbs(page),
                    "confluence_version": version,
                    "schema_version": self.settings.schema_version,
                    "source": page,
                    "source_text": source_text,
                    "parent_text": source_text,
                },
                chunks,
                self.settings.model_version,
            )
            self._write_legacy_page(page_id, section, chunks, old_section)
            self._update_state(page_id, section, version, old_section)

    def process_one(self) -> bool:
        events = self.store.lease_ingest_events(
            self.worker_id, lease_seconds=self.lease_seconds
        )
        if not events:
            return False
        event = events[0]
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            interval = max(1.0, self.lease_seconds / 3)
            while not heartbeat_stop.wait(interval):
                if not self.store.heartbeat_ingest_event(
                    event["id"], self.worker_id, self.lease_seconds
                ):
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            self.process(event)
            if not self.store.complete_ingest_event(event["id"], self.worker_id):
                raise RuntimeError("ingest event lease expired before completion")
        except Exception as exc:
            try:
                self.store.fail_ingest_event(
                    event["id"],
                    self.worker_id,
                    str(exc),
                    max_attempts=self.max_attempts,
                    base_backoff=self.base_backoff,
                )
            except Exception:
                logging.exception("could not fail ingest event %s", event["id"])
            logging.exception("ingest event %s failed", event["id"])
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
        return True

    def run(
        self, *, once: bool = False, max_events: Optional[int] = None, poll_seconds: float = 2
    ) -> int:
        processed = 0
        while not self.stop_event.is_set():
            found = self.process_one()
            if found:
                processed += 1
                if once or (max_events is not None and processed >= max_events):
                    break
            elif once or max_events is not None:
                break
            else:
                self.stop_event.wait(poll_seconds)
        return processed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Drain durable Confluence ingest events")
    parser.add_argument("--base")
    parser.add_argument("--sqlite")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-events", type=int)
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    store = CanonicalStore(args.sqlite or settings.sqlite_path)
    processor = IngestEventProcessor(
        store,
        settings,
        ConfluenceClient(settings),
        lease_seconds=settings.ingest_lease_seconds,
        max_attempts=settings.ingest_max_attempts,
        base_backoff=settings.ingest_base_backoff,
    )
    signal.signal(signal.SIGTERM, processor.stop)
    signal.signal(signal.SIGINT, processor.stop)
    try:
        processor.run(
            once=args.once, max_events=args.max_events, poll_seconds=args.poll_seconds
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
