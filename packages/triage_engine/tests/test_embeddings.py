from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from triage_engine.embeddings import CachedEmbedder, OpenAIEmbedder, get_default_embedder


class _FakeEmbeddingsApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def create(self, *, model: str, input: list[str]) -> Any:
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


def test_get_default_embedder_requires_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRIAGE_ENGINE_EMBEDDER", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        get_default_embedder()


def test_get_default_embedder_rejects_non_openai_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TRIAGE_ENGINE_EMBEDDER", "hashing")

    with pytest.raises(ValueError, match="supports only OpenAI"):
        get_default_embedder()


def test_get_default_embedder_uses_openai_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TRIAGE_ENGINE_EMBEDDER", "openai:text-embedding-3-large")
    monkeypatch.setitem(
        sys.modules,
        "openai",
        types.SimpleNamespace(OpenAI=_FakeOpenAI),
    )

    embedder = get_default_embedder()
    assert isinstance(embedder, CachedEmbedder)
    assert isinstance(embedder.inner, OpenAIEmbedder)
    assert embedder.inner.model == "text-embedding-3-large"


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
    assert embedder._client.embeddings.calls == [
        ("text-embedding-3-small", ["aa", "bbb"]),
        ("text-embedding-3-small", ["c"]),
    ]
