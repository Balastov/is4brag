"""SQLite source of truth for pages, chunks, embeddings, and index work."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import threading
from typing import Iterator, Mapping, Optional, Sequence, Union


FILTER_FIELDS = {
    "section": "section",
    "page_id": "page_id",
    "title": "title",
    "content_type": "json_extract(metadata_json, '$.content_type')",
    "breadcrumbs": "breadcrumbs",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class StoreError(RuntimeError):
    pass


class CanonicalStore:
    """Small sqlite3 repository with explicit transactions and migrations."""

    def __init__(self, path: Union[Path, str], *, fts_fallback: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fts_fallback = fts_fallback
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.fts_available = False
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CanonicalStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def migrate(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        if not self._migration_applied(1):
            with self.transaction() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS pages (
                      page_id TEXT PRIMARY KEY,
                      section TEXT NOT NULL,
                      title TEXT NOT NULL DEFAULT '',
                      url TEXT NOT NULL DEFAULT '',
                      breadcrumbs TEXT NOT NULL DEFAULT '',
                      confluence_version INTEGER NOT NULL DEFAULT 0,
                      schema_version TEXT NOT NULL,
                      source_json TEXT NOT NULL DEFAULT '{}',
                      deleted_at TEXT,
                      updated_at TEXT NOT NULL
                    )"""
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS pages_section_idx ON pages(section, deleted_at)"
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS chunks (
                      chunk_id TEXT PRIMARY KEY,
                      page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
                      section TEXT NOT NULL,
                      title TEXT NOT NULL DEFAULT '',
                      url TEXT NOT NULL DEFAULT '',
                      breadcrumbs TEXT NOT NULL DEFAULT '',
                      text TEXT NOT NULL,
                      chunk_index INTEGER NOT NULL DEFAULT 0,
                      duplicate_ordinal INTEGER NOT NULL DEFAULT 0,
                      content_hash TEXT NOT NULL,
                      chunker_version TEXT NOT NULL,
                      schema_version TEXT NOT NULL,
                      metadata_json TEXT NOT NULL DEFAULT '{}',
                      updated_at TEXT NOT NULL
                    )"""
                )
                db.execute("CREATE INDEX IF NOT EXISTS chunks_page_idx ON chunks(page_id)")
                db.execute("CREATE INDEX IF NOT EXISTS chunks_hash_idx ON chunks(content_hash)")
                db.execute(
                    """CREATE TABLE IF NOT EXISTS embedding_cache (
                      content_hash TEXT NOT NULL,
                      model_version TEXT NOT NULL,
                      dimensions INTEGER NOT NULL,
                      vector_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      PRIMARY KEY(content_hash, model_version)
                    )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS index_jobs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      operation TEXT NOT NULL CHECK(operation IN ('upsert','delete')),
                      chunk_id TEXT NOT NULL,
                      page_id TEXT NOT NULL,
                      model_version TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','leased','completed','dead')),
                      attempts INTEGER NOT NULL DEFAULT 0,
                      available_at TEXT NOT NULL,
                      leased_by TEXT,
                      lease_expires_at TEXT,
                      last_error TEXT,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      UNIQUE(chunk_id, model_version)
                    )"""
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_queue_idx "
                    "ON index_jobs(status, available_at, lease_expires_at, id)"
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS sync_runs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      started_at TEXT NOT NULL,
                      finished_at TEXT,
                      status TEXT NOT NULL,
                      section TEXT,
                      pages_updated INTEGER NOT NULL DEFAULT 0,
                      pages_deleted INTEGER NOT NULL DEFAULT 0,
                      chunks_written INTEGER NOT NULL DEFAULT 0,
                      error TEXT
                    )"""
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                    (utcnow(),),
                )
        if not self._migration_applied(2):
            with self.transaction() as db:
                cache_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(embedding_cache)")
                }
                if "provider_runtime" not in cache_columns:
                    db.execute(
                        "ALTER TABLE embedding_cache ADD COLUMN provider_runtime TEXT NOT NULL "
                        "DEFAULT ''"
                    )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS index_targets (
                      model_version TEXT PRIMARY KEY,
                      provider_runtime TEXT NOT NULL,
                      dimensions INTEGER NOT NULL,
                      collection_name TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )"""
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
                    (utcnow(),),
                )
        if not self._migration_applied(3):
            with self.transaction() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS ingest_events (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      delivery_id TEXT NOT NULL UNIQUE,
                      page_id TEXT NOT NULL,
                      event_type TEXT NOT NULL,
                      payload_json TEXT NOT NULL DEFAULT '{}',
                      status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','leased','completed','dead')),
                      attempts INTEGER NOT NULL DEFAULT 0,
                      available_at TEXT NOT NULL,
                      leased_by TEXT,
                      lease_expires_at TEXT,
                      last_error TEXT,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      completed_at TEXT
                    )"""
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS ingest_events_queue_idx "
                    "ON ingest_events(status, available_at, lease_expires_at, id)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)",
                    (utcnow(),),
                )
        if not self._migration_applied(4):
            with self.transaction() as db:
                page_columns = {row[1] for row in db.execute("PRAGMA table_info(pages)")}
                if "parent_text" not in page_columns:
                    db.execute(
                        "ALTER TABLE pages ADD COLUMN parent_text TEXT NOT NULL DEFAULT ''"
                    )
                if "source_text" not in page_columns:
                    db.execute(
                        "ALTER TABLE pages ADD COLUMN source_text TEXT NOT NULL DEFAULT ''"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)",
                    (utcnow(),),
                )
        if not self._migration_applied(5):
            with self.transaction() as db:
                job_columns = {row[1] for row in db.execute("PRAGMA table_info(index_jobs)")}
                if "generation" not in job_columns:
                    db.execute(
                        "ALTER TABLE index_jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 1"
                    )
                if "replay_required" not in job_columns:
                    db.execute(
                        "ALTER TABLE index_jobs ADD COLUMN replay_required INTEGER NOT NULL DEFAULT 0"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)",
                    (utcnow(),),
                )
        try:
            self.connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                "chunk_id UNINDEXED, title, breadcrumbs, text)"
            )
            self.fts_available = True
            count = self.connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            if count == 0:
                self.connection.execute(
                    "INSERT INTO chunks_fts(chunk_id,title,breadcrumbs,text) "
                    "SELECT chunk_id,title,breadcrumbs,text FROM chunks"
                )
        except sqlite3.OperationalError as exc:
            self.fts_available = False
            if not self.fts_fallback:
                raise StoreError("SQLite was built without FTS5 support") from exc

    def _migration_applied(self, version: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            is not None
        )

    def _enqueue(
        self,
        db: sqlite3.Connection,
        operation: str,
        chunk_id: str,
        page_id: str,
        model_version: str,
    ) -> None:
        now = utcnow()
        db.execute(
            """INSERT INTO index_jobs
               (operation,chunk_id,page_id,model_version,status,available_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(chunk_id,model_version) DO UPDATE SET
                 operation=excluded.operation,
                 page_id=excluded.page_id,
                 generation=index_jobs.generation+1,
                 replay_required=0,
                 status='pending',
                 attempts=0,
                 available_at=excluded.available_at,
                 leased_by=NULL,
                 lease_expires_at=NULL,
                 last_error=NULL,
                 updated_at=excluded.updated_at""",
            (operation, chunk_id, page_id, model_version, "pending", now, now, now),
        )

    def enqueue(
        self, operation: str, chunk_id: str, page_id: str, model_version: str
    ) -> None:
        with self.transaction() as db:
            self._enqueue(db, operation, chunk_id, page_id, model_version)

    def replace_page(
        self,
        page: Mapping[str, object],
        chunks: Sequence[Mapping[str, object]],
        model_version: str,
    ) -> None:
        """Atomically replace all chunks and enqueue vector mutations."""
        page_id = str(page.get("page_id") or page.get("id") or "")
        if not page_id:
            raise ValueError("page_id is required")
        version_value = page.get("confluence_version", page.get("version", 0))
        if isinstance(version_value, Mapping):
            version_value = version_value.get("number", 0)
        now = utcnow()
        with self.transaction() as db:
            old_ids = [
                row[0] for row in db.execute(
                    "SELECT chunk_id FROM chunks WHERE page_id=?", (page_id,)
                )
            ]
            db.execute(
                """INSERT INTO pages(page_id,section,title,url,breadcrumbs,confluence_version,
                   schema_version,source_json,deleted_at,updated_at,parent_text,source_text)
                   VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?)
                   ON CONFLICT(page_id) DO UPDATE SET section=excluded.section,
                   title=excluded.title,url=excluded.url,breadcrumbs=excluded.breadcrumbs,
                   confluence_version=excluded.confluence_version,
                   schema_version=excluded.schema_version,source_json=excluded.source_json,
                   deleted_at=NULL,updated_at=excluded.updated_at,
                   parent_text=excluded.parent_text,source_text=excluded.source_text""",
                (
                    page_id,
                    str(page.get("section", "")),
                    str(page.get("title", "")),
                    str(page.get("url", "")),
                    str(page.get("breadcrumbs", "")),
                    int(version_value or 0),
                    str(page.get("schema_version", "1")),
                    json.dumps(page.get("source", {}), ensure_ascii=False),
                    now,
                    str(page.get("parent_text", page.get("source_text", ""))),
                    str(page.get("source_text", page.get("parent_text", ""))),
                ),
            )
            if self.fts_available:
                db.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id IN "
                    "(SELECT chunk_id FROM chunks WHERE page_id=?)",
                    (page_id,),
                )
            db.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
            new_ids = set()
            for chunk in chunks:
                chunk_id = str(chunk["chunk_id"])
                if chunk_id in new_ids:
                    raise ValueError("duplicate chunk_id in page replacement: " + chunk_id)
                new_ids.add(chunk_id)
                metadata = {
                    key: value
                    for key, value in chunk.items()
                    if key not in {
                        "chunk_id", "page_id", "section", "title", "url", "breadcrumbs",
                        "text", "chunk_index", "duplicate_ordinal", "content_hash",
                        "chunker_version", "schema_version",
                    }
                }
                text = str(chunk.get("text", ""))
                exact_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                values = (
                    chunk_id, page_id, str(chunk.get("section", page.get("section", ""))),
                    str(chunk.get("title", "")), str(chunk.get("url", "")),
                    str(chunk.get("breadcrumbs", "")), text,
                    int(chunk.get("chunk_index", 0)), int(chunk.get("duplicate_ordinal", 0)),
                    exact_hash, str(chunk.get("chunker_version", "1")),
                    str(chunk.get("schema_version", "1")),
                    json.dumps(metadata, ensure_ascii=False), now,
                )
                db.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
                )
                if self.fts_available:
                    db.execute(
                        "INSERT INTO chunks_fts(chunk_id,title,breadcrumbs,text) VALUES(?,?,?,?)",
                        (chunk_id, values[3], values[5], values[6]),
                    )
                self._enqueue(db, "upsert", chunk_id, page_id, model_version)
            for chunk_id in set(old_ids) - new_ids:
                self._enqueue(db, "delete", chunk_id, page_id, model_version)

    def tombstone_page(self, page_id: str, model_version: str) -> int:
        now = utcnow()
        with self.transaction() as db:
            old_ids = [
                row[0] for row in db.execute(
                    "SELECT chunk_id FROM chunks WHERE page_id=?", (str(page_id),)
                )
            ]
            if self.fts_available:
                db.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id IN "
                    "(SELECT chunk_id FROM chunks WHERE page_id=?)", (str(page_id),)
                )
            db.execute("DELETE FROM chunks WHERE page_id=?", (str(page_id),))
            db.execute(
                "UPDATE pages SET deleted_at=?,updated_at=? WHERE page_id=?",
                (now, now, str(page_id)),
            )
            for chunk_id in old_ids:
                self._enqueue(db, "delete", chunk_id, str(page_id), model_version)
        return len(old_ids)

    def get_chunk(self, chunk_id: str) -> Optional[dict]:
        row = self.connection.execute(
            "SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(json.loads(result.pop("metadata_json")))
        return result

    @staticmethod
    def _filter_sql(filters: Optional[Mapping[str, object]], alias: str = "") -> tuple[str, list]:
        clauses = []
        params: list[object] = []
        prefix = alias + "." if alias else ""
        for key, value in (filters or {}).items():
            if key not in FILTER_FIELDS:
                raise ValueError("unsupported filter: %s" % key)
            if not isinstance(value, str) or not value or len(value) > 500:
                raise ValueError("filter %s must be a non-empty string" % key)
            expression = FILTER_FIELDS[key]
            if expression.isidentifier():
                expression = prefix + expression
            elif alias:
                expression = expression.replace("metadata_json", prefix + "metadata_json")
            clauses.append(expression + "=?")
            params.append(value)
        return (" AND " + " AND ".join(clauses) if clauses else ""), params

    def lexical_search(
        self,
        query: str,
        limit: int = 10,
        section: Optional[str] = None,
        filters: Optional[Mapping[str, object]] = None,
    ) -> list[dict]:
        exact_filters = dict(filters or {})
        if section:
            exact_filters["section"] = section
        if self.fts_available:
            sql = (
                "SELECT c.*, bm25(chunks_fts) AS score FROM chunks_fts "
                "JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id WHERE chunks_fts MATCH ?"
            )
            params: list[object] = [query]
            clause, filter_params = self._filter_sql(exact_filters, "c")
            sql += clause
            params.extend(filter_params)
            sql += " ORDER BY score LIMIT ?"
            params.append(limit)
            try:
                return [dict(row) for row in self.connection.execute(sql, params)]
            except sqlite3.OperationalError as exc:
                raise StoreError("invalid FTS5 query: %s" % query) from exc
        if not self.fts_fallback:
            raise StoreError("FTS5 is unavailable")
        pattern = "%" + query.replace("%", r"\%").replace("_", r"\_") + "%"
        sql = (
            "SELECT *, 0.0 AS score FROM chunks WHERE "
            "(text LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')"
        )
        params = [pattern, pattern]
        clause, filter_params = self._filter_sql(exact_filters)
        sql += clause
        params.extend(filter_params)
        sql += " LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.connection.execute(sql, params)]

    def lease_jobs(
        self,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 300,
        model_version: Optional[str] = None,
    ) -> list[dict]:
        now = utcnow()
        expires = _future(lease_seconds)
        with self.transaction() as db:
            model_clause = " AND model_version=?" if model_version else ""
            params: list[object] = [now, now]
            if model_version:
                params.append(model_version)
            params.append(limit)
            rows = db.execute(
                """SELECT id FROM index_jobs
                   WHERE ((status='pending' AND available_at<=?)
                      OR (status='leased' AND lease_expires_at<=?))"""
                + model_clause
                + " ORDER BY id LIMIT ?",
                params,
            ).fetchall()
            ids = [row[0] for row in rows]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            db.execute(
                "UPDATE index_jobs SET status='leased',leased_by=?,lease_expires_at=?,"
                "updated_at=? WHERE id IN (%s)" % marks,
                [worker_id, expires, now] + ids,
            )
            return [
                dict(row) for row in db.execute(
                    "SELECT * FROM index_jobs WHERE id IN (%s) ORDER BY id" % marks, ids
                )
            ]

    def heartbeat(self, job_id: int, worker_id: str, lease_seconds: int = 300) -> bool:
        with self._lock:
            result = self.connection.execute(
                "UPDATE index_jobs SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND status='leased' AND leased_by=?",
                (_future(lease_seconds), utcnow(), job_id, worker_id),
            )
        return result.rowcount == 1

    def job_is_current(self, job_id: int, worker_id: str, generation: int) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM index_jobs WHERE id=? AND status='leased' "
                "AND leased_by=? AND generation=?",
                (job_id, worker_id, generation),
            ).fetchone()
        return row is not None

    def require_job_replay(self, job_id: int, stale_generation: int) -> None:
        """Ensure a newer desired state is replayed after a stale external mutation."""
        now = utcnow()
        with self.transaction() as db:
            row = db.execute(
                "SELECT generation,status FROM index_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or int(row["generation"]) == int(stale_generation):
                return
            if row["status"] == "completed":
                db.execute(
                    "UPDATE index_jobs SET status='pending',attempts=0,available_at=?,"
                    "leased_by=NULL,lease_expires_at=NULL,last_error=NULL,"
                    "replay_required=0,updated_at=? WHERE id=?",
                    (now, now, job_id),
                )
            else:
                db.execute(
                    "UPDATE index_jobs SET replay_required=1,updated_at=? WHERE id=?",
                    (now, job_id),
                )

    def complete_job(
        self, job_id: int, worker_id: str, generation: Optional[int] = None
    ) -> bool:
        now = utcnow()
        with self.transaction() as db:
            params: list[object] = [job_id, worker_id]
            generation_clause = ""
            if generation is not None:
                generation_clause = " AND generation=?"
                params.append(generation)
            row = db.execute(
                "SELECT replay_required FROM index_jobs WHERE id=? AND status='leased' "
                "AND leased_by=?" + generation_clause,
                params,
            ).fetchone()
            if row is None:
                return False
            replay = bool(row["replay_required"])
            db.execute(
                "UPDATE index_jobs SET status=?,available_at=?,leased_by=NULL,"
                "lease_expires_at=NULL,replay_required=0,updated_at=? WHERE id=?",
                ("pending" if replay else "completed", now, now, job_id),
            )
        return True

    def fail_job(
        self,
        job_id: int,
        worker_id: str,
        error: str,
        *,
        max_attempts: int = 5,
        base_backoff: float = 5,
    ) -> str:
        with self.transaction() as db:
            row = db.execute(
                "SELECT attempts FROM index_jobs WHERE id=? AND status='leased' AND leased_by=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                raise StoreError("job lease is not owned by worker")
            attempts = int(row[0]) + 1
            status = "dead" if attempts >= max_attempts else "pending"
            available = utcnow() if status == "dead" else _future(base_backoff * (2 ** (attempts - 1)))
            db.execute(
                "UPDATE index_jobs SET status=?,attempts=?,available_at=?,leased_by=NULL,"
                "lease_expires_at=NULL,last_error=?,updated_at=? WHERE id=?",
                (status, attempts, available, error[:2000], utcnow(), job_id),
            )
        return status

    def queue_metrics(self) -> dict:
        result = {"pending": 0, "leased": 0, "completed": 0, "dead": 0}
        result.update(
            {
                row["status"]: row["count"]
                for row in self.connection.execute(
                    "SELECT status,count(*) AS count FROM index_jobs GROUP BY status"
                )
            }
        )
        result["due"] = self.connection.execute(
            "SELECT count(*) FROM index_jobs WHERE status='pending' AND available_at<=?",
            (utcnow(),),
        ).fetchone()[0]
        return result

    def enqueue_ingest_event(
        self,
        delivery_id: str,
        page_id: str,
        event_type: str,
        payload: Optional[Mapping[str, object]] = None,
    ) -> bool:
        """Persist a redacted event; return False when delivery was already seen."""
        now = utcnow()
        with self._lock:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO ingest_events
                   (delivery_id,page_id,event_type,payload_json,status,available_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    str(delivery_id),
                    str(page_id),
                    str(event_type),
                    json.dumps(dict(payload or {}), ensure_ascii=False),
                    "pending",
                    now,
                    now,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def lease_ingest_events(
        self, worker_id: str, limit: int = 1, lease_seconds: int = 300
    ) -> list[dict]:
        now = utcnow()
        expires = _future(lease_seconds)
        with self.transaction() as db:
            rows = db.execute(
                """SELECT event.id FROM ingest_events AS event
                   WHERE (
                     (event.status='pending' AND event.available_at<=?)
                     OR (event.status='leased' AND event.lease_expires_at<=?)
                   )
                   AND NOT EXISTS (
                     SELECT 1 FROM ingest_events AS earlier
                     WHERE earlier.page_id=event.page_id
                       AND earlier.id<event.id
                       AND earlier.status IN ('pending','leased')
                   )
                   ORDER BY event.id LIMIT ?""",
                (now, now, max(1, int(limit))),
            ).fetchall()
            ids = [row[0] for row in rows]
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            db.execute(
                "UPDATE ingest_events SET status='leased',leased_by=?,lease_expires_at=?,"
                "updated_at=? WHERE id IN (%s)" % marks,
                [worker_id, expires, now] + ids,
            )
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM ingest_events WHERE id IN (%s) ORDER BY id" % marks, ids
                )
            ]

    def complete_ingest_event(self, event_id: int, worker_id: str) -> bool:
        now = utcnow()
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE ingest_events SET status='completed',leased_by=NULL,"
                "lease_expires_at=NULL,completed_at=?,updated_at=? "
                "WHERE id=? AND status='leased' AND leased_by=?",
                (now, now, event_id, worker_id),
            )
        return cursor.rowcount == 1

    def heartbeat_ingest_event(
        self, event_id: int, worker_id: str, lease_seconds: int = 300
    ) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE ingest_events SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND status='leased' AND leased_by=?",
                (_future(lease_seconds), utcnow(), event_id, worker_id),
            )
        return cursor.rowcount == 1

    def fail_ingest_event(
        self,
        event_id: int,
        worker_id: str,
        error: str,
        *,
        max_attempts: int = 5,
        base_backoff: float = 5,
    ) -> str:
        with self.transaction() as db:
            row = db.execute(
                "SELECT attempts FROM ingest_events "
                "WHERE id=? AND status='leased' AND leased_by=?",
                (event_id, worker_id),
            ).fetchone()
            if row is None:
                raise StoreError("ingest event lease is not owned by worker")
            attempts = int(row[0]) + 1
            status = "dead" if attempts >= max_attempts else "pending"
            available = utcnow() if status == "dead" else _future(
                base_backoff * (2 ** (attempts - 1))
            )
            db.execute(
                "UPDATE ingest_events SET status=?,attempts=?,available_at=?,leased_by=NULL,"
                "lease_expires_at=NULL,last_error=?,updated_at=? WHERE id=?",
                (status, attempts, available, str(error)[:2000], utcnow(), event_id),
            )
        return status

    def ingest_event_metrics(self) -> dict:
        result = {"pending": 0, "leased": 0, "completed": 0, "dead": 0}
        result.update(
            {
                row["status"]: row["count"]
                for row in self.connection.execute(
                    "SELECT status,count(*) AS count FROM ingest_events GROUP BY status"
                )
            }
        )
        oldest = self.connection.execute(
            "SELECT created_at FROM ingest_events "
            "WHERE status IN ('pending','leased') ORDER BY created_at LIMIT 1"
        ).fetchone()
        age = 0.0
        if oldest:
            try:
                age = max(
                    0.0,
                    (datetime.now(timezone.utc) - datetime.fromisoformat(oldest[0])).total_seconds(),
                )
            except (TypeError, ValueError):
                age = 0.0
        result["depth"] = result["pending"] + result["leased"]
        result["oldest_age_seconds"] = age
        return result

    def get_embedding(self, content_hash: str, model_version: str) -> Optional[list[float]]:
        record = self.get_embedding_record(content_hash, model_version)
        return None if record is None else record["vector"]

    def get_embedding_record(
        self, content_hash: str, model_version: str
    ) -> Optional[dict]:
        row = self.connection.execute(
            "SELECT vector_json,dimensions,provider_runtime FROM embedding_cache "
            "WHERE content_hash=? AND model_version=?",
            (content_hash, model_version),
        ).fetchone()
        if row is None:
            return None
        return {
            "vector": [float(value) for value in json.loads(row["vector_json"])],
            "dimensions": int(row["dimensions"]),
            "provider_runtime": str(row["provider_runtime"]),
        }

    def put_embedding(
        self,
        content_hash: str,
        model_version: str,
        vector: Sequence[float],
        provider_runtime: str = "",
    ) -> None:
        values = [float(value) for value in vector]
        self.connection.execute(
            "INSERT OR REPLACE INTO embedding_cache"
            "(content_hash,model_version,dimensions,vector_json,created_at,provider_runtime) "
            "VALUES(?,?,?,?,?,?)",
            (
                content_hash,
                model_version,
                len(values),
                json.dumps(values),
                utcnow(),
                provider_runtime,
            ),
        )

    def register_index_target(
        self,
        model_version: str,
        provider_runtime: str,
        dimensions: int,
        collection_name: str,
    ) -> None:
        now = utcnow()
        self.connection.execute(
            """INSERT INTO index_targets
               (model_version,provider_runtime,dimensions,collection_name,created_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(model_version) DO UPDATE SET
                 provider_runtime=excluded.provider_runtime,
                 dimensions=excluded.dimensions,
                 collection_name=excluded.collection_name,
                 updated_at=excluded.updated_at""",
            (
                model_version,
                provider_runtime,
                dimensions,
                collection_name,
                now,
                now,
            ),
        )

    def requeue_model_version(self, model_version: str) -> int:
        """Open one upsert job per current chunk for an explicit target version."""
        rows = self.connection.execute("SELECT chunk_id,page_id FROM chunks").fetchall()
        with self.transaction() as db:
            for row in rows:
                self._enqueue(db, "upsert", row["chunk_id"], row["page_id"], model_version)
        return len(rows)

    def drift_counts(self, vector_count: Optional[int] = None) -> dict:
        chunks = self.connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        pages = self.connection.execute(
            "SELECT count(*) FROM pages WHERE deleted_at IS NULL"
        ).fetchone()[0]
        pending = self.connection.execute(
            "SELECT count(*) FROM index_jobs WHERE status IN ('pending','leased','dead')"
        ).fetchone()[0]
        uncached = self.connection.execute(
            "SELECT count(*) FROM chunks c WHERE NOT EXISTS "
            "(SELECT 1 FROM embedding_cache e WHERE e.content_hash=c.content_hash)"
        ).fetchone()[0]
        result = {
            "pages": pages,
            "chunks": chunks,
            "uncached_chunks": uncached,
            "unsettled_jobs": pending,
        }
        if vector_count is not None:
            result.update(
                {"vectors": vector_count, "vector_delta": int(vector_count) - int(chunks)}
            )
        return result

    def start_sync_run(self, section: Optional[str] = None) -> int:
        cursor = self.connection.execute(
            "INSERT INTO sync_runs(started_at,status,section) VALUES(?,?,?)",
            (utcnow(), "running", section),
        )
        return int(cursor.lastrowid)

    def finish_sync_run(self, run_id: int, status: str = "completed", **metrics: object) -> None:
        allowed = {"pages_updated", "pages_deleted", "chunks_written", "error"}
        values = {key: value for key, value in metrics.items() if key in allowed}
        assignments = ["finished_at=?", "status=?"] + ["%s=?" % key for key in values]
        self.connection.execute(
            "UPDATE sync_runs SET %s WHERE id=?" % ",".join(assignments),
            [utcnow(), status] + list(values.values()) + [run_id],
        )
