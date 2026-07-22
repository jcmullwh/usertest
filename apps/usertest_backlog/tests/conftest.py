from __future__ import annotations

import importlib

import pytest

_LIFECYCLE_CONTEXT_ENV_NAMES = (
    "USERTEST_LIFECYCLE_CONTEXT",
    "USERTEST_LIFECYCLE_CONTEXT_FILE",
)


@pytest.fixture(autouse=True)
def _isolate_process_lifecycle_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests hermetic when a verified controller launches pytest."""
    for name in _LIFECYCLE_CONTEXT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _force_local_triage_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep usertest_backlog tests offline and deterministic."""
    try:
        triage_embeddings = importlib.import_module("triage_engine.embeddings")
        triage_similarity = importlib.import_module("triage_engine.similarity")
    except ModuleNotFoundError:
        # Older/alternate triage_engine installations may not expose these modules.
        return

    hashing_cls = None
    try:
        triage_testing = importlib.import_module("triage_engine.testing")
        hashing_cls = getattr(triage_testing, "HashingEmbedder", None)
    except ModuleNotFoundError:
        hashing_cls = getattr(triage_embeddings, "HashingEmbedder", None)

    cached_cls = getattr(triage_embeddings, "CachedEmbedder", None)
    if hashing_cls is None or cached_cls is None:
        return

    embedder = cached_cls(hashing_cls())
    monkeypatch.setattr(triage_embeddings, "get_default_embedder", lambda: embedder)
    monkeypatch.setattr(triage_similarity, "get_default_embedder", lambda: embedder)
