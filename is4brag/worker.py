"""Durable SQLite-to-Qdrant indexing worker."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
from typing import Optional

from .config import Settings
from .embeddings import OnnxEmbeddingProvider, SentenceTransformerProvider
from .qdrant import QdrantAdapter, versioned_collection
from .store import CanonicalStore


class IndexWorker:
    def __init__(
        self,
        store: CanonicalStore,
        provider,
        vector_store,
        *,
        worker_id: Optional[str] = None,
        lease_seconds: int = 300,
        heartbeat_seconds: int = 60,
        max_attempts: int = 5,
        base_backoff: float = 5,
    ) -> None:
        self.store = store
        self.provider = provider
        self.vector_store = vector_store
        self.worker_id = worker_id or "%s:%d" % (socket.gethostname(), os.getpid())
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.stop_event = threading.Event()

    def stop(self, *_args: object) -> None:
        self.stop_event.set()

    def process_one(self) -> bool:
        jobs = self.store.lease_jobs(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            model_version=self.provider.model_version,
        )
        if not jobs:
            return False
        job = jobs[0]
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(self.heartbeat_seconds):
                if not self.store.heartbeat(
                    job["id"], self.worker_id, self.lease_seconds
                ):
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            generation = int(job.get("generation", 1))
            if not self.store.job_is_current(
                job["id"], self.worker_id, generation
            ):
                return True
            if job["operation"] == "delete":
                self.vector_store.delete(job["chunk_id"])
            else:
                chunk = self.store.get_chunk(job["chunk_id"])
                if chunk is None:
                    # A replacement can supersede a queued upsert before it is leased.
                    self.vector_store.delete(job["chunk_id"])
                else:
                    cached = self.store.get_embedding_record(
                        chunk["content_hash"], job["model_version"]
                    )
                    if cached is None:
                        vector = self.provider.embed_documents([chunk["text"]])[0]
                        runtime = self.provider.runtime
                        self.store.put_embedding(
                            chunk["content_hash"],
                            job["model_version"],
                            vector,
                            self.provider.runtime,
                        )
                    else:
                        vector = cached["vector"]
                        runtime = cached["provider_runtime"] or self.provider.runtime
                    self.vector_store.upsert(chunk, vector, job["model_version"], runtime)
            if not self.store.job_is_current(
                job["id"], self.worker_id, generation
            ):
                self.store.require_job_replay(job["id"], generation)
                return True
            if not self.store.complete_job(job["id"], self.worker_id, generation):
                self.store.require_job_replay(job["id"], generation)
                raise RuntimeError("job lease expired before completion")
        except Exception as exc:
            try:
                self.store.fail_job(
                    job["id"],
                    self.worker_id,
                    str(exc),
                    max_attempts=self.max_attempts,
                    base_backoff=self.base_backoff,
                )
            except Exception:
                logging.exception("could not fail job %s", job["id"])
            logging.exception("index job %s failed", job["id"])
        finally:
            heartbeat_stop.set()
            thread.join(timeout=1)
        return True

    def run(
        self,
        *,
        once: bool = False,
        max_jobs: Optional[int] = None,
        poll_seconds: float = 2,
    ) -> int:
        processed = 0
        self.vector_store.ensure_collection()
        while not self.stop_event.is_set():
            found = self.process_one()
            if found:
                processed += 1
                if once or (max_jobs is not None and processed >= max_jobs):
                    break
            elif once or max_jobs is not None:
                break
            else:
                self.stop_event.wait(poll_seconds)
        return processed


def build_provider(settings: Settings, *, device: Optional[str] = None):
    if settings.embedding_provider == "pytorch":
        return SentenceTransformerProvider(
            settings.model_name,
            device=device or settings.embedding_device,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
            model_version=settings.model_version,
        )
    if settings.embedding_provider == "onnx":
        if not settings.onnx_model_path or not settings.onnx_tokenizer_path:
            raise ValueError(
                "IS4BRAG_ONNX_MODEL_PATH and IS4BRAG_ONNX_TOKENIZER_PATH are required"
            )
        return OnnxEmbeddingProvider(
            settings.onnx_model_path,
            settings.onnx_tokenizer_path,
            model_version=settings.model_version,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
            intra_op_threads=settings.onnx_intra_op_threads,
            quantization=settings.onnx_quantization,
        )
    raise ValueError("unsupported embedding provider: %s" % settings.embedding_provider)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Index SQLite chunks into Qdrant")
    parser.add_argument("--base")
    parser.add_argument("--sqlite")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-jobs", type=int)
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--device")
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.base)
    store = CanonicalStore(args.sqlite or settings.sqlite_path)
    provider = build_provider(settings, device=args.device)
    collection = versioned_collection(settings.qdrant_collection, provider.model_version)
    vectors = QdrantAdapter(
        settings.qdrant_url,
        collection,
        api_key=settings.qdrant_api_key,
        dimensions=provider.dimensions,
    )
    store.register_index_target(
        provider.model_version, provider.runtime, provider.dimensions, collection
    )
    worker = IndexWorker(store, provider, vectors)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    try:
        worker.run(once=args.once, max_jobs=args.max_jobs, poll_seconds=args.poll_seconds)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
