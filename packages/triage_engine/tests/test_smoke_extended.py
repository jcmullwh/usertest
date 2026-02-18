from __future__ import annotations

import importlib.util
import os

import pytest

from triage_engine.embeddings import OpenAIEmbedder


def test_openai_embedding_live_smoke() -> None:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        pytest.skip("OPENAI_API_KEY is not set")
    if importlib.util.find_spec("openai") is None:
        pytest.skip("openai package is not installed in this environment")

    model = (os.getenv("TRIAGE_ENGINE_OPENAI_MODEL") or "text-embedding-3-small").strip()
    base_url = (os.getenv("OPENAI_BASE_URL") or "").strip() or None

    embedder = OpenAIEmbedder(model=model, api_key=api_key, base_url=base_url)
    vectors = embedder.embed_texts(["triage_engine smoke_extended live embedding check"])
    assert len(vectors) == 1
    assert isinstance(vectors[0], list)
    assert len(vectors[0]) > 0
