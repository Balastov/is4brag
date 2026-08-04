"""Central, environment-driven configuration."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict, Optional


DEFAULT_SECTIONS = {
    "Термины и сокращения": "1933357",
    "Управление проектом": "1933362",
    "Стадии проекта": "1933363",
    "Архитектура": "2820058",
    "KISU Metro - Спецификации требований": "1933456",
}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    """Runtime settings. No secrets or host paths are hard-coded by callers."""

    base_path: Path
    sandbox_path: Path
    confluence_url: str = "https://conf-metro.ibs.ru"
    confluence_pat: str = ""
    model_name: str = "intfloat/multilingual-e5-large"
    model_version: str = "intfloat/multilingual-e5-large"
    embedding_provider: str = "pytorch"
    embedding_dimensions: int = 1024
    embedding_batch_size: int = 32
    embedding_device: str = "cpu"
    onnx_model_path: str = ""
    onnx_tokenizer_path: str = ""
    onnx_intra_op_threads: int = 0
    onnx_quantization: str = ""
    chunk_size: int = 800
    chunk_overlap: int = 150
    min_chunk_len: int = 100
    chunker_version: str = "3"
    chunk_strategy: str = "auto"
    schema_version: str = "2"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "kisu_metro"
    qdrant_alias: str = "kisu_metro_active"
    sqlite_path: Path = Path("is4brag.sqlite3")
    request_timeout: int = 30
    workers: int = 5
    search_api_bind: str = "127.0.0.1"
    search_api_port: int = 8080
    search_api_url: str = ""
    search_timeout: float = 15.0
    search_concurrency: int = 4
    search_candidate_limit: int = 30
    search_semantic_weight: float = 0.6
    search_lexical_weight: float = 0.4
    search_admin_token: str = ""
    webhook_secret: str = ""
    webhook_max_bytes: int = 262144
    webhook_allowed_cidrs: tuple[str, ...] = ()
    ingest_lease_seconds: int = 300
    ingest_max_attempts: int = 5
    ingest_base_backoff: float = 5.0
    sections: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SECTIONS))

    @classmethod
    def from_env(cls, base_path: Optional[str] = None) -> "Settings":
        base = Path(base_path or os.getenv("KISU_METRO_BASE") or os.getcwd()).expanduser()
        sandbox = Path(os.getenv("IS4BRAG_SANDBOX_PATH", str(base))).expanduser()
        sqlite_raw = os.getenv("IS4BRAG_SQLITE_PATH", "is4brag.sqlite3")
        sqlite_path = Path(sqlite_raw).expanduser()
        if not sqlite_path.is_absolute():
            sqlite_path = base / sqlite_path
        return cls(
            base_path=base,
            sandbox_path=sandbox,
            confluence_url=os.getenv("CONFLUENCE_URL", "https://conf-metro.ibs.ru").rstrip("/"),
            confluence_pat=os.getenv("CONFLUENCE_PAT", ""),
            model_name=os.getenv("IS4BRAG_MODEL_NAME", "intfloat/multilingual-e5-large"),
            model_version=os.getenv(
                "IS4BRAG_MODEL_VERSION",
                os.getenv("IS4BRAG_MODEL_NAME", "intfloat/multilingual-e5-large"),
            ),
            embedding_provider=os.getenv("IS4BRAG_EMBEDDING_PROVIDER", "pytorch").lower(),
            embedding_dimensions=_env_int("IS4BRAG_EMBEDDING_DIMENSIONS", 1024),
            embedding_batch_size=_env_int("IS4BRAG_EMBEDDING_BATCH_SIZE", 32),
            embedding_device=os.getenv("IS4BRAG_EMBEDDING_DEVICE", "cpu"),
            onnx_model_path=os.getenv("IS4BRAG_ONNX_MODEL_PATH", ""),
            onnx_tokenizer_path=os.getenv("IS4BRAG_ONNX_TOKENIZER_PATH", ""),
            onnx_intra_op_threads=_env_int("IS4BRAG_ONNX_INTRA_OP_THREADS", 0),
            onnx_quantization=os.getenv("IS4BRAG_ONNX_QUANTIZATION", ""),
            chunk_size=_env_int("IS4BRAG_CHUNK_SIZE", 800),
            chunk_overlap=_env_int("IS4BRAG_CHUNK_OVERLAP", 150),
            min_chunk_len=_env_int("IS4BRAG_MIN_CHUNK_LEN", 100),
            chunker_version=os.getenv("IS4BRAG_CHUNKER_VERSION", "3"),
            chunk_strategy=os.getenv("IS4BRAG_CHUNK_STRATEGY", "auto").lower(),
            schema_version=os.getenv("IS4BRAG_SCHEMA_VERSION", "2"),
            qdrant_url=os.getenv("QDRANT_URL", "http://localhost:6333"),
            qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
            qdrant_collection=os.getenv("QDRANT_COLLECTION", "kisu_metro"),
            qdrant_alias=os.getenv("QDRANT_ALIAS", "kisu_metro_active"),
            sqlite_path=sqlite_path,
            request_timeout=_env_int("CONFLUENCE_TIMEOUT", 30),
            workers=_env_int("IS4BRAG_WORKERS", 5),
            search_api_bind=os.getenv("SEARCH_API_BIND", "127.0.0.1"),
            search_api_port=_env_int("SEARCH_API_PORT", 8080),
            search_api_url=os.getenv("SEARCH_API_URL", "").rstrip("/"),
            search_timeout=_env_float("SEARCH_API_TIMEOUT", 15.0),
            search_concurrency=max(1, _env_int("SEARCH_API_CONCURRENCY", 4)),
            search_candidate_limit=max(1, _env_int("SEARCH_CANDIDATE_LIMIT", 30)),
            search_semantic_weight=_env_float("SEARCH_SEMANTIC_WEIGHT", 0.6),
            search_lexical_weight=_env_float("SEARCH_LEXICAL_WEIGHT", 0.4),
            search_admin_token=os.getenv("SEARCH_ADMIN_TOKEN", ""),
            webhook_secret=os.getenv("CONFLUENCE_WEBHOOK_SECRET", ""),
            webhook_max_bytes=max(1, _env_int("CONFLUENCE_WEBHOOK_MAX_BYTES", 262144)),
            webhook_allowed_cidrs=tuple(
                item.strip()
                for item in os.getenv("CONFLUENCE_WEBHOOK_ALLOWED_CIDRS", "").split(",")
                if item.strip()
            ),
            ingest_lease_seconds=max(1, _env_int("IS4BRAG_INGEST_LEASE_SECONDS", 300)),
            ingest_max_attempts=max(1, _env_int("IS4BRAG_INGEST_MAX_ATTEMPTS", 5)),
            ingest_base_backoff=max(0, _env_float("IS4BRAG_INGEST_BASE_BACKOFF", 5.0)),
        )
