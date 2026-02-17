from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _force_local_triage_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep usertest_backlog tests offline and deterministic."""
    try:
        triage_embeddings = importlib.import_module("triage_engine.embeddings")
        triage_similarity = importlib.import_module("triage_engine.similarity")
    except ModuleNotFoundError:
        # Older/alternate triage_engine installations may not expose these modules.
        return

    if not hasattr(triage_embeddings, "HashingEmbedder") or not hasattr(
        triage_embeddings, "CachedEmbedder"
    ):
        return

    embedder = triage_embeddings.CachedEmbedder(triage_embeddings.HashingEmbedder())
    monkeypatch.setattr(triage_embeddings, "get_default_embedder", lambda: embedder)
    monkeypatch.setattr(triage_similarity, "get_default_embedder", lambda: embedder)
