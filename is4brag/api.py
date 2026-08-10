"""Long-running search API with lazy optional web-framework imports."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import threading
import time
from typing import Optional

from .config import Settings
from .ingest import WebhookError, WebhookMetrics, WebhookService
from .qdrant import QdrantAdapter
from .search import SearchCore
from .store import CanonicalStore, FILTER_FIELDS
from .worker import build_provider

# FastAPI resolves string annotations for nested route handlers against this
# module globals. Keep Request/Header here (not only inside create_app), or
# POST /webhooks/confluence becomes a bogus required query param "request".
try:
    from fastapi import FastAPI, Header, Request
except ImportError:  # optional extra is4brag[api]
    FastAPI = None  # type: ignore[misc, assignment]
    Header = None  # type: ignore[misc, assignment]
    Request = None  # type: ignore[misc, assignment]


class SearchMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.errors = 0
        self.rejected = 0
        self.in_flight = 0
        self.duration = 0.0

    def begin(self) -> float:
        with self._lock:
            self.requests += 1
            self.in_flight += 1
        return time.perf_counter()

    def finish(self, started: float, *, error: bool = False) -> None:
        with self._lock:
            self.in_flight -= 1
            self.duration += time.perf_counter() - started
            self.errors += int(error)

    def render(self) -> str:
        with self._lock:
            values = {
                "is4brag_search_requests_total": self.requests,
                "is4brag_search_errors_total": self.errors,
                "is4brag_search_rejected_total": self.rejected,
                "is4brag_search_in_flight": self.in_flight,
                "is4brag_search_duration_seconds_total": self.duration,
            }
        return "".join("%s %s\n" % item for item in values.items())


def build_core(settings: Settings) -> SearchCore:
    store = CanonicalStore(settings.sqlite_path)
    provider = build_provider(settings)
    vectors = QdrantAdapter(
        settings.qdrant_url,
        settings.qdrant_alias or settings.qdrant_collection,
        api_key=settings.qdrant_api_key,
        dimensions=provider.dimensions,
    )
    return SearchCore(
        store,
        provider,
        vectors,
        semantic_weight=settings.search_semantic_weight,
        lexical_weight=settings.search_lexical_weight,
        candidate_limit=settings.search_candidate_limit,
        active_alias=settings.qdrant_alias,
    )


def create_app(
    settings: Optional[Settings] = None,
    *,
    core: Optional[SearchCore] = None,
    webhook_store: Optional[CanonicalStore] = None,
    warm_on_startup: bool = True,
):
    """Create an app; importing this module does not require FastAPI."""
    if FastAPI is None or Request is None or Header is None:
        raise RuntimeError("FastAPI is optional; install is4brag[api]")
    try:
        from fastapi.responses import JSONResponse, PlainTextResponse
    except ImportError as exc:
        raise RuntimeError("FastAPI is optional; install is4brag[api]") from exc

    configured = settings or Settings.from_env()
    owned_core = core is None
    owned_webhook_store = webhook_store is None
    search_core = core or build_core(configured)
    event_store = webhook_store or CanonicalStore(configured.sqlite_path)
    metrics = SearchMetrics()
    webhook_metrics = WebhookMetrics()
    webhook = WebhookService(
        event_store,
        configured.webhook_secret,
        max_bytes=configured.webhook_max_bytes,
        allowed_cidrs=configured.webhook_allowed_cidrs,
        metrics=webhook_metrics,
    )
    semaphore = asyncio.Semaphore(configured.search_concurrency)
    app = FastAPI(title="IS4B RAG Search API", version="1")
    app.state.search_core = search_core
    app.state.metrics = metrics
    app.state.webhook = webhook

    @app.on_event("startup")
    async def startup() -> None:
        if warm_on_startup:
            try:
                await asyncio.to_thread(search_core.warm)
            except Exception as exc:
                # Readiness exposes model failure without killing liveness.
                import logging

                logging.getLogger("is4brag.api").exception(
                    "search model warm-up failed: %s", exc
                )
                return

    @app.on_event("shutdown")
    async def shutdown() -> None:
        if owned_core:
            search_core.store.close()
        if owned_webhook_store:
            event_store.close()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        checks = await asyncio.to_thread(search_core.status)
        status = 200 if all(checks.values()) else 503
        return JSONResponse({"status": "ready" if status == 200 else "not_ready", "checks": checks}, status)

    @app.get("/metrics")
    async def prometheus_metrics():
        output = metrics.render()
        for name, value in webhook_metrics.snapshot().items():
            output += "is4brag_webhook_%s_total %s\n" % (name, value)
        queue = event_store.ingest_event_metrics()
        output += "is4brag_ingest_queue_depth %s\n" % queue["depth"]
        output += "is4brag_ingest_queue_oldest_age_seconds %s\n" % queue[
            "oldest_age_seconds"
        ]
        output += "is4brag_ingest_processed_total %s\n" % queue["completed"]
        output += "is4brag_ingest_dead_total %s\n" % queue["dead"]
        return PlainTextResponse(output, media_type="text/plain; version=0.0.4")

    @app.post("/webhooks/confluence")
    async def confluence_webhook(request: Request):
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > configured.webhook_max_bytes:
                    webhook_metrics.increment("rejected_size")
                    return JSONResponse(
                        {"error": {"code": "payload_too_large", "message": "request rejected"}},
                        413,
                    )
            except ValueError:
                return JSONResponse(
                    {"error": {"code": "invalid_request", "message": "request rejected"}},
                    400,
                )
        chunks = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > configured.webhook_max_bytes:
                webhook_metrics.increment("rejected_size")
                return JSONResponse(
                    {"error": {"code": "payload_too_large", "message": "request rejected"}},
                    413,
                )
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            result = await asyncio.to_thread(
                webhook.handle,
                body,
                dict(request.headers),
                request.client.host if request.client else None,
            )
        except WebhookError as exc:
            return JSONResponse(
                {"error": {"code": exc.code, "message": "request rejected"}},
                exc.status,
            )
        return JSONResponse(result, 202)

    @app.post("/search")
    async def search(request: dict):
        query = request.get("query")
        top_k = request.get("top_k", 10)
        sections = request.get("sections")
        if request.get("section") and not sections:
            sections = [request["section"]]
        filters = request.get("filters") or {}
        use_parents = request.get("use_parents", True)
        if not isinstance(query, str) or not query.strip():
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "query must not be empty"}},
                422,
            )
        if sections is not None and (
            not isinstance(sections, list)
            or not all(isinstance(value, str) and value for value in sections)
        ):
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": "sections must be strings"}},
                422,
            )
        if (
            not isinstance(filters, dict)
            or any(key not in FILTER_FIELDS for key in filters)
            or any(
                not isinstance(value, str) or not value or len(value) > 500
                for value in filters.values()
            )
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "filters must use supported exact-match string fields",
                    }
                },
                422,
            )
        started = metrics.begin()
        error = False
        acquired = False
        release_in_finally = True
        try:
            # Fail quickly when all worker slots remain occupied.
            await asyncio.wait_for(semaphore.acquire(), timeout=configured.search_timeout)
            acquired = True
            worker = asyncio.create_task(
                asyncio.to_thread(
                    search_core.search,
                    query,
                    top_k=int(top_k),
                    sections=sections,
                    use_parents=bool(use_parents),
                    filters=filters,
                )
            )
            try:
                results = await asyncio.wait_for(
                    asyncio.shield(worker), timeout=configured.search_timeout
                )
            except asyncio.TimeoutError:
                # A running thread cannot be cancelled. Keep its slot until it
                # actually exits so timed-out work cannot exceed the bound.
                release_in_finally = False

                def release_slot(task):
                    try:
                        task.exception()
                    except asyncio.CancelledError:
                        pass
                    semaphore.release()

                worker.add_done_callback(release_slot)
                raise
            return {"query": query, "results": results}
        except asyncio.TimeoutError:
            error = True
            metrics.rejected += int(not acquired)
            return JSONResponse(
                {"error": {"code": "timeout", "message": "search deadline exceeded"}},
                504,
            )
        except (TypeError, ValueError) as exc:
            error = True
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": str(exc)}},
                422,
            )
        except Exception:
            error = True
            return JSONResponse(
                {"error": {"code": "search_unavailable", "message": "search backend unavailable"}},
                503,
            )
        finally:
            if acquired and release_in_finally:
                semaphore.release()
            metrics.finish(started, error=error)

    @app.post("/admin/reload")
    async def reload_search(
        authorization: Optional[str] = Header(default=None),
        x_admin_token: Optional[str] = Header(default=None),
    ):
        supplied = x_admin_token or (
            authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        )
        expected = configured.search_admin_token
        if not expected or not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "admin token required"}},
                401,
            )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(search_core.reload), timeout=configured.search_timeout
            )
        except Exception:
            return JSONResponse(
                {"error": {"code": "reload_failed", "message": "search reload failed"}},
                503,
            )
        return {"status": "reloaded"}

    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the IS4B search API")
    parser.add_argument("--base")
    parser.add_argument("--bind")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is optional; install is4brag[api]") from exc
    uvicorn.run(
        create_app(settings),
        host=args.bind or settings.search_api_bind,
        port=args.port or settings.search_api_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
