from __future__ import annotations

import sqlite3
import sys
import types
from collections.abc import Sequence
from pathlib import Path

import pytest

from triage_engine.embeddings import (
    CachedEmbedder,
    DiskCachedEmbedder,
    OpenAIEmbedder,
    get_default_embedder,
)


class _FakeEmbeddingsApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def create(self, *, model: str, input: list[str]) -> object:
        self.calls.append((model, list(input)))
        data = [
            types.SimpleNamespace(index=idx, embedding=[float(idx), float(len(text))])
            for idx, text in enumerate(input)
        ]
        data.reverse()
        return types.SimpleNamespace(data=data)


class _FakeOpenAI:
    def __init__(self, **_: object) -> None:
        self.embeddings = _FakeEmbeddingsApi()


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(text)), float(len(text) % 3)] for text in texts]


def test_get_default_embedder_requires_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRIAGE_ENGINE_EMBEDDER", raising=False)
    monkeypatch.delenv("TRIAGE_ENGINE_EMBED_CACHE_PATH", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_default_embedder()


def test_get_default_embedder_rejects_non_openai_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TRIAGE_ENGINE_EMBEDDER", "hashing")
    monkeypatch.delenv("TRIAGE_ENGINE_EMBED_CACHE_PATH", raising=False)

    with pytest.raises(ValueError, match="supports only OpenAI"):
        get_default_embedder()


def test_get_default_embedder_uses_openai_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TRIAGE_ENGINE_EMBEDDER", "openai:text-embedding-3-large")
    monkeypatch.delenv("TRIAGE_ENGINE_EMBED_CACHE_PATH", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(OpenAI=_FakeOpenAI),
    )

    embedder = get_default_embedder()
    assert isinstance(embedder, CachedEmbedder)
    assert isinstance(embedder.inner, OpenAIEmbedder)
    assert embedder.inner.model == "text-embedding-3-large"


def test_get_default_embedder_uses_disk_cache_when_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache_path = tmp_path / "embed_cache.sqlite3"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TRIAGE_ENGINE_EMBEDDER", "openai:text-embedding-3-large")
    monkeypatch.setenv("TRIAGE_ENGINE_EMBED_CACHE_PATH", str(cache_path))
    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(OpenAI=_FakeOpenAI),
    )

    embedder = get_default_embedder()
    assert isinstance(embedder, DiskCachedEmbedder)
    assert isinstance(embedder.inner, CachedEmbedder)
    assert isinstance(embedder.inner.inner, OpenAIEmbedder)
    assert embedder.inner.inner.model == "text-embedding-3-large"


def test_disk_cached_embedder_reuses_cached_vectors(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.sqlite3"
    inner = _CountingEmbedder()
    embedder = DiskCachedEmbedder(inner=inner, path=str(cache_path))

    first = embedder.embed_texts(["alpha", "beta", "alpha"])
    assert inner.calls == 1
    assert first[0] == first[2]

    second = embedder.embed_texts(["beta", "alpha"])
    assert inner.calls == 1
    assert second == [first[1], first[0]]

    third = embedder.embed_texts(["alpha", "gamma"])
    assert inner.calls == 2
    assert third[0] == first[0]

    with sqlite3.connect(cache_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()
        assert count is not None
        assert int(count[0]) == 3


def test_openai_embedder_batches_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(OpenAI=_FakeOpenAI),
    )

    embedder = OpenAIEmbedder(model="text-embedding-3-small", api_key="test-key", batch_size=2)
    vectors = embedder.embed_texts(["aa", "bbb", "c"])
    assert vectors == [[0.0, 2.0], [1.0, 3.0], [0.0, 1.0]]
    embeddings_api = embedder._client.embeddings
    assert isinstance(embeddings_api, _FakeEmbeddingsApi)
    assert embeddings_api.calls == [
        ("text-embedding-3-small", ["aa", "bbb"]),
        ("text-embedding-3-small", ["c"]),
    ]
