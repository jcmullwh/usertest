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


def dedupe_clusters(
    items: Sequence[T],
    *,
    get_title: Callable[[T], str],
    get_text_chunks: Callable[[T], Iterable[str]],
    get_evidence_ids: Callable[[T], Sequence[str]] | None = None,
    title_similarity_threshold: float = 0.93,
    overall_similarity_threshold: float = 0.90,
    min_evidence_overlap: int = 2,
    include_singletons: bool = True,
    embedder: Embedder | None = None,
) -> list[list[int]]:
    """Find conservative near-duplicate clusters.

    This function is stricter than ``cluster_items``. It is used to collapse duplicates, not to
    build broad topical clusters.

    The implementation is embedding-first, with two additional high-precision signals:
    - exact fingerprint matches (identical normalized text)
    - near-identical title token sets (useful for very short items)
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

    emb_threshold = float(title_similarity_threshold)
    overall_threshold = float(overall_similarity_threshold)

    for i, j in sorted(candidate_pairs):
        sim = compute_pair_similarity(vectors[i], vectors[j])

        strong_evidence = sim.evidence_overlap >= int(min_evidence_overlap)
        # For dedupe, anchors are treated as supporting evidence, not sufficient on their own.
        strong_anchor = sim.anchor_jaccard >= 0.30
        strong_embedding = sim.embedding_similarity >= emb_threshold
        strong_overall = sim.overall_similarity >= overall_threshold

        near_identical_title = sim.title_jaccard >= 0.95

        duplicate = (
            sim.exact_duplicate
            or strong_evidence
            or (near_identical_title and sim.embedding_similarity >= 0.65)
            or (strong_embedding and strong_anchor)
            or (strong_overall and strong_embedding)
        )

        if duplicate:
            _union(i, j)

    clusters_by_root: dict[int, list[int]] = {}
    for idx in range(len(items)):
        clusters_by_root.setdefault(_find(idx), []).append(idx)

    clusters = [sorted(indices) for indices in clusters_by_root.values()]
    clusters.sort(key=lambda component: component[0])

    if include_singletons:
        return clusters
    return [component for component in clusters if len(component) > 1]
