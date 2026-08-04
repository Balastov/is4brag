"""Embedding provider contracts and lazy E5 runtimes."""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import Optional, Protocol, Sequence


E5_DOCUMENT_PREFIX = "passage: "
E5_QUERY_PREFIX = "query: "


def _prefix(texts: Sequence[str], prefix: str) -> list[str]:
    return [prefix + (text or "") for text in texts]


class EmbeddingProvider(Protocol):
    model_version: str
    dimensions: int
    runtime: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class SentenceTransformerProvider:
    def __init__(
        self,
        model_name: str = "intfloat/multilingual-e5-large",
        *,
        device: str = "cpu",
        dimensions: int = 1024,
        batch_size: int = 32,
        model_version: Optional[str] = None,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version or model_name
        self.device = device
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.runtime = "sentence-transformers/pytorch"
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is required; install is4brag[index]"
                ) from exc
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def _encode(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._load().encode(
            _prefix(texts, prefix),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        result = [[float(value) for value in vector] for vector in vectors]
        if result and len(result[0]) != self.dimensions:
            raise ValueError(
                "model returned %d dimensions, expected %d"
                % (len(result[0]), self.dimensions)
            )
        return result

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, E5_DOCUMENT_PREFIX)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, E5_QUERY_PREFIX)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Backward-compatible document embedding entrypoint."""
        return self.embed_documents(texts)


class OnnxEmbeddingProvider:
    """Adapter for a local exported transformer ONNX model.

    The model must accept tokenizer outputs and return ``last_hidden_state`` (or
    the first output). Quantized INT8 models work without special casing: point
    ``model_path`` at the quantized artifact.
    """

    def __init__(
        self,
        model_path: str,
        tokenizer_path: str,
        *,
        model_version: str,
        dimensions: int = 1024,
        batch_size: int = 32,
        max_length: int = 512,
        providers: Optional[Sequence[str]] = None,
        intra_op_threads: int = 0,
        quantization: str = "",
    ) -> None:
        self.model_path = Path(model_path)
        self.tokenizer_path = Path(tokenizer_path)
        self.model_version = model_version
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.max_length = max_length
        self.providers = list(providers or ["CPUExecutionProvider"])
        self.intra_op_threads = intra_op_threads
        self.quantization = quantization.lower().strip()
        self.runtime = (
            "onnxruntime/%s" % self.quantization
            if self.quantization
            else "onnxruntime/fp32"
        )
        self._tokenizer = None
        self._session = None

    def _load(self):
        if self._session is None:
            if not self.model_path.is_file():
                raise FileNotFoundError("exported ONNX model not found: %s" % self.model_path)
            if not self.tokenizer_path.exists():
                raise FileNotFoundError("local tokenizer not found: %s" % self.tokenizer_path)
            try:
                import onnxruntime as ort
                from transformers import AutoTokenizer
            except ImportError as exc:
                raise RuntimeError(
                    "onnxruntime and transformers are required; install is4brag[onnx]"
                ) from exc
            options = ort.SessionOptions()
            if self.intra_op_threads:
                options.intra_op_num_threads = self.intra_op_threads
            self._session = ort.InferenceSession(
                str(self.model_path), sess_options=options, providers=self.providers
            )
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(self.tokenizer_path), local_files_only=True
            )
        return self._tokenizer, self._session

    def _encode(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("numpy is required; install is4brag[onnx]") from exc
        tokenizer, session = self._load()
        results: list[list[float]] = []
        input_names = {item.name for item in session.get_inputs()}
        for start in range(0, len(texts), self.batch_size):
            encoded = tokenizer(
                _prefix(texts[start : start + self.batch_size], prefix),
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np",
            )
            inputs = {
                name: np.asarray(value, dtype=np.int64)
                for name, value in encoded.items()
                if name in input_names
            }
            hidden = np.asarray(session.run(None, inputs)[0])
            if hidden.ndim == 2:
                pooled = hidden
            elif hidden.ndim == 3:
                mask = np.asarray(encoded["attention_mask"], dtype=np.float32)[..., None]
                pooled = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
            else:
                raise ValueError("ONNX model returned an unsupported output rank")
            pooled /= np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
            if pooled.ndim != 2 or pooled.shape[1] != self.dimensions:
                raise ValueError(
                    "model returned %s dimensions, expected %d"
                    % (pooled.shape[1] if pooled.ndim == 2 else "invalid", self.dimensions)
                )
            results.extend([[float(value) for value in row] for row in pooled])
        return results

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, E5_DOCUMENT_PREFIX)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, E5_QUERY_PREFIX)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_documents(texts)


class DeterministicFakeProvider:
    """Network-free provider suitable for deterministic worker tests."""

    def __init__(self, dimensions: int = 8, model_version: str = "fake-v1") -> None:
        self.dimensions = dimensions
        self.model_version = model_version
        self.runtime = "deterministic-fake"
        self.calls = 0
        self.texts_embedded = 0
        self.inputs: list[str] = []

    def _embed(self, texts: Sequence[str], prefix: str) -> list[list[float]]:
        self.calls += 1
        self.texts_embedded += len(texts)
        result = []
        prefixed = _prefix(texts, prefix)
        self.inputs.extend(prefixed)
        for text in prefixed:
            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
            randomizer = random.Random(seed)
            vector = [randomizer.uniform(-1, 1) for _ in range(self.dimensions)]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            result.append([value / norm for value in vector])
        return result

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, E5_DOCUMENT_PREFIX)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, E5_QUERY_PREFIX)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_documents(texts)
