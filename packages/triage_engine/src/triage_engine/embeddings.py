from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from triage_engine.text import tokenize

__all__ = [
    "Embedder",
    "get_default_embedder",
    "CachedEmbedder",
    "DiskCachedEmbedder",
    "HashingEmbedder",
    "SentenceTransformersEmbedder",
    "OpenAIEmbedder",
    "dot",
    "l2_normalize",
    "cosine_similarity",
]


def _stable_hash64(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("dot() requires vectors of the same length")
    return sum(x * y for x, y in zip(a, b, strict=True))


def l2_normalize(vec: Sequence[float]) -> tuple[float, ...]:
    norm_sq = sum(v * v for v in vec)
    if norm_sq <= 0.0:
        return tuple(0.0 for _ in vec)
    inv = 1.0 / math.sqrt(norm_sq)
    return tuple(v * inv for v in vec)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1].

    Notes
    -----
    This function does not assume inputs are normalized.
    """

    if len(a) != len(b):
        raise ValueError("cosine_similarity() requires vectors of the same length")

    aa = sum(v * v for v in a)
    bb = sum(v * v for v in b)
    if aa <= 0.0 or bb <= 0.0:
        return 0.0
    return dot(a, b) / math.sqrt(aa * bb)


@runtime_checkable
class Embedder(Protocol):
    """Embedding provider interface.

    The triage engine treats embeddings as an interchangeable dependency. Any caller-provided
    embedder that matches this protocol can be used.
    """

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError


@dataclass
class CachedEmbedder:
    """Cache wrapper for any embedder.

    Embedding backends (especially remote APIs) are expensive. This wrapper provides a
    deterministic in-memory cache keyed by SHA-256(text).
    """

    inner: Embedder

    def __post_init__(self) -> None:
        self._cache: dict[str, list[float]] = {}

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        missing: list[str] = []
        missing_keys: list[str] = []
        keys: list[str] = []

        for text in texts:
            key = hashlib.sha256(text.encode("utf-8")).hexdigest()
            keys.append(key)
            if key not in self._cache:
                missing.append(text)
                missing_keys.append(key)

        if missing:
            vectors = self.inner.embed_texts(missing)
            if len(vectors) != len(missing_keys):
                raise ValueError(
                    "Embedding backend returned unexpected vector count: "
                    f"expected {len(missing_keys)}, got {len(vectors)}"
                )
            for key, vec in zip(missing_keys, vectors, strict=True):
                self._cache[key] = list(vec)

        return [list(self._cache[key]) for key in keys]


def _embedder_model_id(embedder: Embedder) -> str:
    current: object = embedder
    visited: set[int] = set()
    while True:
        current_id = id(current)
        if current_id in visited:
            break
        visited.add(current_id)

        for attribute in ("model", "model_name"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        inner = getattr(current, "inner", None)
        if inner is None:
            break
        current = inner
    return type(embedder).__name__


@dataclass
class DiskCachedEmbedder:
    """SQLite cache wrapper for any embedder.

    Vectors are stored as JSON arrays keyed by model identifier and exact text hash.
    """

    inner: Embedder
    path: str

    def _connect(self) -> sqlite3.Connection:
        db_path = os.fspath(self.path)
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                model_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                PRIMARY KEY (model_id, text_hash)
            )
            """
        )
        return conn

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        model_id = _embedder_model_id(self.inner)
        hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]

        unique_text_by_hash: dict[str, str] = {}
        ordered_unique_hashes: list[str] = []
        for text, text_hash in zip(texts, hashes, strict=True):
            if text_hash not in unique_text_by_hash:
                unique_text_by_hash[text_hash] = text
                ordered_unique_hashes.append(text_hash)

        cached_vectors: dict[str, list[float]] = {}
        with self._connect() as conn:
            placeholders = ", ".join("?" for _ in ordered_unique_hashes)
            rows = conn.execute(
                (
                    "SELECT text_hash, vector_json FROM embedding_cache "
                    f"WHERE model_id = ? AND text_hash IN ({placeholders})"
                ),
                [model_id, *ordered_unique_hashes],
            ).fetchall()
            for text_hash, vector_json in rows:
                cached_vectors[str(text_hash)] = list(map(float, json.loads(vector_json)))

            missing_hashes = [
                text_hash for text_hash in ordered_unique_hashes if text_hash not in cached_vectors
            ]
            if missing_hashes:
                missing_texts = [unique_text_by_hash[text_hash] for text_hash in missing_hashes]
                missing_vectors = self.inner.embed_texts(missing_texts)
                if len(missing_vectors) != len(missing_hashes):
                    raise ValueError(
                        "Embedding backend returned unexpected vector count: "
                        f"expected {len(missing_hashes)}, got {len(missing_vectors)}"
                    )

                rows_to_insert: list[tuple[str, str, str]] = []
                for text_hash, vector in zip(missing_hashes, missing_vectors, strict=True):
                    normalized = [float(value) for value in vector]
                    cached_vectors[text_hash] = normalized
                    rows_to_insert.append(
                        (
                            model_id,
                            text_hash,
                            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
                conn.executemany(
                    (
                        "INSERT OR IGNORE INTO embedding_cache(model_id, text_hash, vector_json) "
                        "VALUES (?, ?, ?)"
                    ),
                    rows_to_insert,
                )
                conn.commit()

        return [list(cached_vectors[text_hash]) for text_hash in hashes]


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _iter_char_ngrams(text: str, *, n: int, max_ngrams: int) -> list[str]:
    cleaned = _NON_ALNUM_RE.sub("", text.lower())
    if not cleaned:
        return []
    if len(cleaned) <= n:
        return [cleaned]

    out: list[str] = []
    limit = max(0, max_ngrams)
    for i in range(len(cleaned) - n + 1):
        out.append(cleaned[i : i + n])
        if limit and len(out) >= limit:
            break
    return out


@dataclass(frozen=True)
class HashingEmbedder:
    """Dependency-free embedding.

    This backend is deterministic and works offline. It is not a neural embedding model.
    It exists so triage logic can operate without external services.

    Implementation
    --------------
    - Feature hashing over word tokens + character n-grams.
    - Signed hashing ("hashing trick") into a fixed-size vector.
    - L2 normalization to make cosine similarity meaningful.
    """

    dim: int = 512
    token_weight: float = 1.0
    ngram_n: int = 3
    ngram_weight: float = 0.5
    max_ngrams: int = 4096

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        dim = int(self.dim)
        if dim <= 0:
            raise ValueError("HashingEmbedder.dim must be > 0")

        for text in texts:
            vec = [0.0] * dim

            # Word tokens.
            for token in tokenize(text):
                h = _stable_hash64(token)
                idx = int(h % dim)
                sign = 1.0 if (h >> 63) & 1 else -1.0
                vec[idx] += sign * self.token_weight

            # Character n-grams (helps with small edits and path-like strings).
            if self.ngram_weight:
                for gram in _iter_char_ngrams(text, n=self.ngram_n, max_ngrams=self.max_ngrams):
                    h = _stable_hash64("g:" + gram)
                    idx = int(h % dim)
                    sign = 1.0 if (h >> 63) & 1 else -1.0
                    vec[idx] += sign * self.ngram_weight

            vectors.append(list(l2_normalize(vec)))

        return vectors


@dataclass
class SentenceTransformersEmbedder:
    """SentenceTransformers embedder (optional dependency)."""

    model_name: str = "all-MiniLM-L6-v2"
    batch_size: int = 32
    normalize: bool = True
    device: str | None = None

    def __post_init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ModuleNotFoundError(
                "sentence-transformers is not installed. Install triage_engine with the "
                "sentence_transformers extra (or add sentence-transformers to your environment)."
            ) from exc

        # Loading the model may download weights if not present.
        self._model = SentenceTransformer(self.model_name, device=self.device)

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        embeddings = self._model.encode(
            list(texts),
            batch_size=int(self.batch_size),
            normalize_embeddings=bool(self.normalize),
            show_progress_bar=False,
        )
        return [list(map(float, row)) for row in embeddings]


@dataclass
class OpenAIEmbedder:
    """OpenAI embeddings backend using the official Python SDK."""

    model: str = "text-embedding-3-small"
    api_key: str | None = None
    base_url: str | None = None
    batch_size: int = 128

    def __post_init__(self) -> None:
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ModuleNotFoundError(
                "openai is not installed. Add openai to your environment."
            ) from exc

        self._client = OpenAI(
            api_key=self.api_key if self.api_key else None,
            base_url=self.base_url if self.base_url else None,
        )

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        batch_size = int(self.batch_size)
        if batch_size <= 0:
            raise ValueError("OpenAIEmbedder.batch_size must be > 0")
        if not texts:
            return []

        out: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            resp = self._client.embeddings.create(model=self.model, input=batch)
            data = getattr(resp, "data", None)
            if data is None:
                raise ValueError("OpenAI embeddings response missing data")
            if len(data) != len(batch):
                raise ValueError(
                    "OpenAI embeddings response returned unexpected item count: "
                    f"expected {len(batch)}, got {len(data)}"
                )

            ordered: list[list[float] | None] = [None] * len(batch)
            for pos, item in enumerate(data):
                embedding = getattr(item, "embedding", None)
                if embedding is None:
                    raise ValueError("OpenAI embeddings response item missing embedding")
                idx = getattr(item, "index", pos)
                if not isinstance(idx, int) or idx < 0 or idx >= len(batch):
                    idx = pos
                if ordered[idx] is not None:
                    raise ValueError("OpenAI embeddings response contains duplicate indices")
                ordered[idx] = list(map(float, embedding))

            if any(vec is None for vec in ordered):
                raise ValueError("OpenAI embeddings response missing indexed rows")
            out.extend([vec for vec in ordered if vec is not None])

        return out


def get_default_embedder() -> Embedder:
    """Select the default embedder.

    This function is intentionally strict: triage runs require remote embeddings and will
    fail fast when OpenAI credentials are missing or invalid.
    """

    spec = (os.getenv("TRIAGE_ENGINE_EMBEDDER") or "").strip()
    spec_lower = spec.lower()

    def _wrap(embedder: Embedder) -> Embedder:
        # Memory cache is always safe and helps both local and remote backends.
        wrapped: Embedder = CachedEmbedder(embedder)
        cache_path = (os.getenv("TRIAGE_ENGINE_EMBED_CACHE_PATH") or "").strip()
        if cache_path:
            wrapped = DiskCachedEmbedder(wrapped, path=cache_path)
        return wrapped

    model = os.getenv("TRIAGE_ENGINE_OPENAI_MODEL") or "text-embedding-3-small"
    if spec_lower:
        if not spec_lower.startswith("openai"):
            raise ValueError(
                "TRIAGE_ENGINE_EMBEDDER currently supports only OpenAI values "
                "('openai' or 'openai:<model>')."
            )
        if ":" in spec:
            _, _, explicit_model = spec.partition(":")
            model = explicit_model.strip() or model

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for triage embeddings. "
            "No offline embedding fallback is available."
        )

    return _wrap(
        OpenAIEmbedder(
            model=model,
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL"),
        )
    )
