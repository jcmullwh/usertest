from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from triage_engine import build_merge_candidates


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
class Finding:
    title: str
    body: str
    evidence_ids: tuple[str, ...]


def test_build_merge_candidates_with_non_ticket_items() -> None:
    items = [
        Finding(
            title="Refine backlog atom parsing for JSON recovery",
            body=(
                "Touches packages/reporter/src/reporter/backlog.py and "
                "improves parser robustness."
            ),
            evidence_ids=("runA:1",),
        ),
        Finding(
            title="Backlog parser recovery improvements",
            body=(
                "Focuses on packages/reporter/src/reporter/backlog.py plus "
                "follow-up validation."
            ),
            evidence_ids=("runB:2",),
        ),
        Finding(
            title="Improve docs for quickstart",
            body="Update README examples only.",
            evidence_ids=("runC:3",),
        ),
    ]

    candidates = build_merge_candidates(
        items,
        get_title=lambda item: item.title,
        get_evidence_ids=lambda item: item.evidence_ids,
        get_text_chunks=lambda item: (item.title, item.body),
        embedder=_DeterministicEmbedder(),
    )

    assert candidates == [(0, 1)]
