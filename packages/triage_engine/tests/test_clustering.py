from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from triage_engine import cluster_items, cluster_items_knn


class _DeterministicEmbedder:
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            if ("parser" in lowered) or ("backlog" in lowered):
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


class _KeywordEmbedder:
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            if "bridge" in lowered:
                vectors.append([0.7, 0.7, 0.0])
            elif any(token in lowered for token in ("parser", "backlog", "triage")):
                vectors.append([1.0, 0.0, 0.0])
            elif any(token in lowered for token in ("docs", "readme", "theme", "typography")):
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


@dataclass(frozen=True)
class PullRequestLike:
    number: int
    title: str
    body: str
    files: tuple[str, ...]


def test_cluster_items_groups_related_prs() -> None:
    prs = [
        PullRequestLike(
            number=101,
            title="Refine backlog parser for malformed JSON",
            body="Improve recovery in parser output handling.",
            files=("packages/reporter/src/reporter/backlog.py",),
        ),
        PullRequestLike(
            number=102,
            title="Backlog parser JSON recovery improvements",
            body="Stabilize parser handling and improve tests.",
            files=("packages/reporter/src/reporter/backlog.py",),
        ),
        PullRequestLike(
            number=200,
            title="Update website docs theme",
            body="Only docs and styling updates.",
            files=("docs/design/architecture.md",),
        ),
    ]

    clusters = cluster_items(
        prs,
        get_title=lambda pr: pr.title,
        get_text_chunks=lambda pr: (pr.title, pr.body, *pr.files),
        embedder=_DeterministicEmbedder(),
    )

    assert clusters == [[0, 1], [2]]


def test_cluster_items_knn_keeps_two_theme_groups_separate() -> None:
    items = [
        PullRequestLike(
            number=1,
            title="Parser failure in backlog triage",
            body="Parser tokens cause backlog merge issues.",
            files=("packages/backlog_core/src/backlog_core/parser.py",),
        ),
        PullRequestLike(
            number=2,
            title="Backlog parser recovers malformed issue body",
            body="Triaging parser logic now handles malformed lines.",
            files=("packages/backlog_core/src/backlog_core/parser.py",),
        ),
        PullRequestLike(
            number=3,
            title="Docs: update theme typography for onboarding",
            body="README/docs visual adjustments only.",
            files=("docs/design/typography.md",),
        ),
        PullRequestLike(
            number=4,
            title="README theme refresh for docs",
            body="Theme and typography pass for docs.",
            files=("README.md",),
        ),
    ]

    clusters = cluster_items_knn(
        items,
        get_title=lambda item: item.title,
        get_text_chunks=lambda item: (item.title, item.body, *item.files),
        embedder=_KeywordEmbedder(),
        k=3,
        overall_similarity_threshold=0.75,
        require_mutual=True,
        refine=False,
    )

    assert clusters == [[0, 1], [2, 3]]


def test_cluster_items_knn_refinement_splits_bridged_cluster() -> None:
    items = [
        PullRequestLike(
            number=10,
            title="Parser backlog issue",
            body="Parser module bug in triage",
            files=("packages/backlog_core/src/backlog_core/parser.py",),
        ),
        PullRequestLike(
            number=11,
            title="Bridge parser docs theme",
            body="Bridge item connecting parser and docs topics",
            files=("docs/design/parser-theme.md",),
        ),
        PullRequestLike(
            number=12,
            title="Docs theme polish",
            body="README typography docs theme cleanup",
            files=("docs/design/typography.md",),
        ),
    ]

    clusters = cluster_items_knn(
        items,
        get_title=lambda item: item.title,
        get_text_chunks=lambda item: (item.title, item.body, *item.files),
        embedder=_KeywordEmbedder(),
        k=2,
        overall_similarity_threshold=0.70,
        require_mutual=False,
        refine=True,
        representative_similarity_threshold=0.90,
        include_singletons=True,
    )

    assert clusters == [[0], [1], [2]]
