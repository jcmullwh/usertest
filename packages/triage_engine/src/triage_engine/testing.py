"""Test-only utilities for triage_engine.

This module intentionally contains *offline* / deterministic embedder backends that are
useful for unit tests and local experiments.

Production triage runs should use real embedding models (see
:func:`triage_engine.embeddings.get_default_embedder`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from triage_engine.embeddings import l2_normalize
from triage_engine.text import tokenize

__all__ = [
    "HashingEmbedder",
    "SentenceTransformersEmbedder",
]


def _stable_hash64(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


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
    """Dependency-free embedding for tests.

    This backend is deterministic and works offline. It is *not* a neural embedding model.
    It exists so triage logic can operate without external services in unit tests.

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
    """SentenceTransformers embedder (optional dependency).

    This backend is useful for local experimentation where calling a remote embedding API
    is undesirable. It is not used by default anywhere in this repo.

    NOTE: Loading the model may download weights the first time it is used.
    """

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
