from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from triage_engine.embeddings import Embedder
from triage_engine.similarity import (
    build_item_vectors,
    compute_pair_similarity,
    generate_candidate_pairs,
)

T = TypeVar("T")


def build_merge_candidates(
    items: Sequence[T],
    *,
    get_title: Callable[[T], str],
    get_evidence_ids: Callable[[T], Sequence[str]],
    get_text_chunks: Callable[[T], Iterable[str]],
    max_candidates: int = 200,
    title_overlap_threshold: float = 0.55,
    embedder: Embedder | None = None,
) -> list[tuple[int, int]]:
    """Build candidate index pairs likely describing the same underlying issue.

    Notes
    -----
    - Generic: callers define how to extract title/evidence/text.
    - Titles are not treated as the primary signal; they contribute as one chunk among many.
    - Candidates are ranked by semantic similarity (embedding cosine + supporting signals).

    Parameters
    ----------
    title_overlap_threshold:
        Compatibility parameter from the initial implementation.

        The engine now uses a semantic similarity score in [0, 1]. This parameter acts as a
        conservative lower-bound filter on that score to avoid emitting clearly unrelated pairs.
    embedder:
        Optional embedding backend.

    Returns
    -------
    list[tuple[int, int]]
        Candidate index pairs ordered by decreasing similarity.
    """

    if not items:
        return []

    vectors = build_item_vectors(
        items,
        get_title=get_title,
        get_text_chunks=get_text_chunks,
        get_evidence_ids=get_evidence_ids,
        embedder=embedder,
    )

    candidate_pairs = generate_candidate_pairs(vectors)

    scored: list[tuple[float, int, int]] = []
    min_score = float(title_overlap_threshold)

    for i, j in sorted(candidate_pairs):
        sim = compute_pair_similarity(vectors[i], vectors[j])

        keep = (
            sim.exact_duplicate
            or sim.evidence_overlap > 0
            or sim.anchor_jaccard > 0.0
            or sim.title_jaccard >= 0.5
            or sim.overall_similarity >= min_score
        )
        if not keep:
            continue

        # Ranking score: prioritize semantic similarity, then corroborating signals.
        score = sim.overall_similarity
        score += 0.03 * min(3, sim.evidence_overlap)
        score += 0.02 * sim.anchor_jaccard
        score += 0.02 * sim.title_jaccard
        scored.append((score, i, j))

    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [(i, j) for _, i, j in scored[: int(max_candidates)]]
