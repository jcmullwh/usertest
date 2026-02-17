from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from triage_engine import cluster_items


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
