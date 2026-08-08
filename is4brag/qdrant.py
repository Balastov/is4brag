"""Optional Qdrant adapter and an in-memory implementation for tests."""

from __future__ import annotations

import re
import hashlib
from typing import Mapping, Optional, Sequence
import uuid


VECTOR_SIZE = 1024


def point_id(chunk_id: str) -> str:
    """Qdrant accepts UUIDs, while canonical chunk IDs are short SHA-256 values."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "is4brag:" + chunk_id))


def versioned_collection(base: str, model_version: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_-]+", "-", model_version).strip("-").lower()
    if not suffix:
        raise ValueError("model_version must contain a collection-safe character")
    digest = hashlib.sha256(model_version.encode("utf-8")).hexdigest()[:10]
    return "%s__%s-%s" % (base, suffix[:68], digest)


def chunk_payload(
    chunk: Mapping[str, object], model_version: str, provider_runtime: str = ""
) -> dict:
    return {
        "chunk_id": str(chunk["chunk_id"]),
        "page_id": str(chunk.get("page_id", "")),
        "section": str(chunk.get("section", "")),
        "title": str(chunk.get("title", "")),
        "url": str(chunk.get("url", "")),
        "breadcrumbs": str(chunk.get("breadcrumbs", "")),
        "content_type": str(chunk.get("content_type", "prose")),
        "text": str(chunk.get("text", "")),
        "content_hash": str(chunk.get("content_hash", "")),
        "chunker_version": str(chunk.get("chunker_version", "")),
        "schema_version": str(chunk.get("schema_version", "")),
        "model_version": model_version,
        "provider_runtime": provider_runtime,
    }


class QdrantAdapter:
    def __init__(
        self,
        url: str,
        collection: str,
        *,
        api_key: str = "",
        dimensions: int = VECTOR_SIZE,
    ) -> None:
        self.url = url
        self.collection = collection
        self.api_key = api_key or None
        self.dimensions = dimensions
        self._client = None
        self._models = None

    def _load(self):
        if self._client is None:
            try:
                from qdrant_client import QdrantClient, models
            except ImportError as exc:
                raise RuntimeError("qdrant-client is required; install is4brag[qdrant]") from exc
            self._client = QdrantClient(url=self.url, api_key=self.api_key)
            self._models = models
        return self._client, self._models

    def ensure_collection(self) -> None:
        client, models = self._load()
        if client.collection_exists(self.collection):
            return
        try:
            client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self.dimensions, distance=models.Distance.COSINE
                ),
            )
        except Exception:
            # Concurrent workers race here on first start; losing the race is
            # only an error if the collection still does not exist afterwards.
            if not client.collection_exists(self.collection):
                raise

    def upsert(
        self,
        chunk: Mapping[str, object],
        vector: Sequence[float],
        model_version: str,
        provider_runtime: str = "",
    ) -> None:
        if len(vector) != self.dimensions:
            raise ValueError("vector dimension mismatch")
        client, models = self._load()
        client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=point_id(str(chunk["chunk_id"])),
                    vector=[float(value) for value in vector],
                    payload=chunk_payload(chunk, model_version, provider_runtime),
                )
            ],
            wait=True,
        )

    def delete(self, chunk_id: str) -> None:
        client, models = self._load()
        client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=[point_id(chunk_id)]),
            wait=True,
        )

    def search(
        self,
        vector: Sequence[float],
        limit: int = 10,
        section: Optional[str] = None,
        filters: Optional[Mapping[str, str]] = None,
    ) -> list[dict]:
        client, models = self._load()
        query_filter = None
        exact_filters = dict(filters or {})
        if section:
            exact_filters["section"] = section
        if exact_filters:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key=key, match=models.MatchValue(value=value)
                    )
                    for key, value in exact_filters.items()
                ]
            )
        if hasattr(client, "query_points"):
            response = client.query_points(
                collection_name=self.collection,
                query=list(vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            points = response.points
        else:  # qdrant-client < 1.10
            points = client.search(
                collection_name=self.collection,
                query_vector=list(vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        return [{"id": str(item.id), "score": item.score, **(item.payload or {})} for item in points]

    def health(self) -> bool:
        try:
            self._load()[0].get_collections()
            return True
        except Exception:
            return False

    def count(self) -> int:
        return int(
            self._load()[0].count(
                collection_name=self.collection, exact=True
            ).count
        )

    def collection_manifest(self, page_size: int = 256) -> dict:
        """Return every canonical point identity plus configured vector dimensions."""
        client, _models = self._load()
        if not client.collection_exists(self.collection):
            return {
                "exists": False,
                "count": 0,
                "identities": [],
                "collection_dimensions": [],
                "point_dimensions": [],
            }
        total = self.count()
        info = client.get_collection(self.collection)
        vectors_config = info.config.params.vectors
        if hasattr(vectors_config, "size"):
            collection_dimensions = [int(vectors_config.size)]
        elif isinstance(vectors_config, Mapping):
            collection_dimensions = sorted(
                {int(value.size) for value in vectors_config.values()}
            )
        else:
            collection_dimensions = []
        identities = []
        offset = None
        seen_offsets = set()
        while True:
            points, next_offset = client.scroll(
                collection_name=self.collection,
                limit=max(1, int(page_size)),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                identities.append(
                    {
                        "chunk_id": str(payload.get("chunk_id", "")),
                        "content_hash": str(payload.get("content_hash", "")),
                        "model_version": str(payload.get("model_version", "")),
                        "chunker_version": str(payload.get("chunker_version", "")),
                    }
                )
            if next_offset is None:
                break
            marker = repr(next_offset)
            if marker in seen_offsets:
                raise RuntimeError("Qdrant scroll returned a repeated offset")
            seen_offsets.add(marker)
            offset = next_offset
        return {
            "exists": True,
            "count": total,
            "identities": identities,
            "collection_dimensions": collection_dimensions,
            # Qdrant enforces the collection vector schema for every point.
            "point_dimensions": collection_dimensions if total else [],
        }

    def create_snapshot(self):
        return self._load()[0].create_snapshot(collection_name=self.collection)

    def alias_target(self, alias: str) -> Optional[str]:
        aliases = self._load()[0].get_aliases().aliases
        for item in aliases:
            if item.alias_name == alias:
                return str(item.collection_name)
        return None

    def promote_alias(self, alias: str, target_collection: str) -> Optional[str]:
        """Atomically switch an alias and return its previous target."""
        client, models = self._load()
        if not client.collection_exists(target_collection):
            raise ValueError("target collection does not exist: %s" % target_collection)
        previous = self.alias_target(alias)
        operations = []
        if previous is not None:
            operations.append(
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=alias)
                )
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=target_collection, alias_name=alias
                )
            )
        )
        client.update_collection_aliases(change_aliases_operations=operations)
        return previous

    def rollback_alias(self, alias: str, previous_collection: str) -> Optional[str]:
        return self.promote_alias(alias, previous_collection)


class FakeQdrantAdapter:
    def __init__(self, dimensions: int = 8, collection: str = "test") -> None:
        self.dimensions = dimensions
        self.collection = collection
        self.collections: dict[str, dict[str, dict]] = {collection: {}}
        self.aliases: dict[str, str] = {}

    @property
    def points(self) -> dict[str, dict]:
        target = self.aliases.get(self.collection, self.collection)
        return self.collections.setdefault(target, {})

    def ensure_collection(self) -> None:
        return None

    def upsert(
        self,
        chunk: Mapping[str, object],
        vector: Sequence[float],
        model_version: str,
        provider_runtime: str = "",
    ) -> None:
        if len(vector) != self.dimensions:
            raise ValueError("vector dimension mismatch")
        self.points[str(chunk["chunk_id"])] = {
            "vector": [float(value) for value in vector],
            "payload": chunk_payload(chunk, model_version, provider_runtime),
        }

    def delete(self, chunk_id: str) -> None:
        self.points.pop(chunk_id, None)

    def health(self) -> bool:
        return True

    def count(self) -> int:
        return len(self.points)

    def collection_manifest(self, page_size: int = 256) -> dict:
        del page_size
        if self.collection not in self.collections:
            return {
                "exists": False,
                "count": 0,
                "identities": [],
                "collection_dimensions": [],
                "point_dimensions": [],
            }
        points = list(self.points.values())
        return {
            "exists": True,
            "count": len(points),
            "identities": [
                {
                    "chunk_id": str(point["payload"].get("chunk_id", "")),
                    "content_hash": str(point["payload"].get("content_hash", "")),
                    "model_version": str(point["payload"].get("model_version", "")),
                    "chunker_version": str(point["payload"].get("chunker_version", "")),
                }
                for point in points
            ],
            "collection_dimensions": [self.dimensions],
            "point_dimensions": sorted({len(point["vector"]) for point in points}),
        }

    def search(
        self,
        vector: Sequence[float],
        limit: int = 10,
        section: Optional[str] = None,
        filters: Optional[Mapping[str, str]] = None,
    ) -> list[dict]:
        scored = []
        exact_filters = dict(filters or {})
        if section:
            exact_filters["section"] = section
        for chunk_id, point in self.points.items():
            if any(
                str(point["payload"].get(key, "")) != value
                for key, value in exact_filters.items()
            ):
                continue
            score = sum(a * b for a, b in zip(vector, point["vector"]))
            scored.append({"id": point_id(chunk_id), "score": score, **point["payload"]})
        return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]

    def alias_target(self, alias: str) -> Optional[str]:
        return self.aliases.get(alias)

    def promote_alias(self, alias: str, target_collection: str) -> Optional[str]:
        if target_collection not in self.collections:
            raise ValueError("target collection does not exist: %s" % target_collection)
        previous = self.aliases.get(alias)
        self.aliases[alias] = target_collection
        return previous

    def rollback_alias(self, alias: str, previous_collection: str) -> Optional[str]:
        return self.promote_alias(alias, previous_collection)
