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


def cluster_items(
    items: Sequence[T],
    *,
    get_title: Callable[[T], str],
    get_text_chunks: Callable[[T], Iterable[str]],
    title_overlap_threshold: float = 0.55,
    embedder: Embedder | None = None,
) -> list[list[int]]:
    """Cluster items using semantic similarity over arbitrary text chunks.

    Parameters
    ----------
    title_overlap_threshold:
        Compatibility parameter from the initial implementation.

        The engine now uses a semantic similarity score in [0, 1]. This parameter is treated as
        the minimum similarity required to create a clustering edge.
    """

    if not items:
        return []

    vectors = build_item_vectors(
        items,
        get_title=get_title,
        get_text_chunks=get_text_chunks,
        get_evidence_ids=None,
        embedder=embedder,
    )

    candidate_pairs = generate_candidate_pairs(vectors)

    parent = list(range(len(items)))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra = _find(a)
        rb = _find(b)
        if ra == rb:
            return
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    threshold = float(title_overlap_threshold)

    for i, j in sorted(candidate_pairs):
        sim = compute_pair_similarity(vectors[i], vectors[j])

        edge = (
            sim.exact_duplicate
            or sim.anchor_jaccard >= 0.20
            or sim.title_jaccard >= 0.55
            or sim.overall_similarity >= threshold
        )
        if edge:
            _union(i, j)

    clusters_by_root: dict[int, list[int]] = {}
    for idx in range(len(items)):
        root = _find(idx)
        clusters_by_root.setdefault(root, []).append(idx)

    clusters = [sorted(indices) for indices in clusters_by_root.values()]
    return sorted(clusters, key=lambda component: (-len(component), component[0]))
