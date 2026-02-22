from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from itertools import combinations
from typing import TypeVar

from triage_engine.embeddings import Embedder, dot
from triage_engine.similarity import (
    PairSimilarity,
    build_item_vectors,
    compute_pair_similarity,
    generate_candidate_pairs,
)

T = TypeVar("T")


def _find(parent: list[int], x: int) -> int:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union(parent: list[int], left: int, right: int) -> None:
    root_left = _find(parent, left)
    root_right = _find(parent, right)
    if root_left == root_right:
        return
    if root_left < root_right:
        parent[root_right] = root_left
    else:
        parent[root_left] = root_right


def _exact_duplicate_pairs(fingerprints: Sequence[str]) -> set[tuple[int, int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for idx, fingerprint in enumerate(fingerprints):
        if fingerprint:
            groups[fingerprint].append(idx)

    out: set[tuple[int, int]] = set()
    for members in groups.values():
        if len(members) > 1:
            for left, right in combinations(sorted(members), 2):
                out.add((left, right))
    return out


def _embedding_similarity(
    left: int,
    right: int,
    *,
    vectors: Sequence[tuple[float, ...]],
    pair_similarity_cache: dict[tuple[int, int], PairSimilarity],
) -> float:
    if left == right:
        return 1.0

    key = (left, right) if left < right else (right, left)
    cached = pair_similarity_cache.get(key)
    if cached is not None:
        return cached.embedding_similarity

    cosine = dot(vectors[left], vectors[right])
    cosine = max(-1.0, min(1.0, cosine))
    return (cosine + 1.0) / 2.0


def _select_medoid_index(
    component: Sequence[int],
    *,
    vectors: Sequence[tuple[float, ...]],
    pair_similarity_cache: dict[tuple[int, int], PairSimilarity],
) -> int:
    if not component:
        raise ValueError("Cannot pick representative from an empty component.")
    if len(component) == 1:
        return component[0]

    best_index = component[0]
    best_score = -1.0
    for candidate in component:
        sims: list[float] = []
        for other in component:
            if other == candidate:
                continue
            sims.append(
                _embedding_similarity(
                    candidate,
                    other,
                    vectors=vectors,
                    pair_similarity_cache=pair_similarity_cache,
                )
            )
        average = sum(sims) / float(len(sims)) if sims else 1.0
        if average > best_score or (average == best_score and candidate < best_index):
            best_index = candidate
            best_score = average
    return best_index


def _cluster_from_edges(item_count: int, edges: Sequence[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(item_count))
    for left, right in sorted(edges):
        _union(parent, left, right)

    components: dict[int, list[int]] = {}
    for idx in range(item_count):
        components.setdefault(_find(parent, idx), []).append(idx)

    out = [sorted(component) for component in components.values()]
    out.sort(key=lambda component: component[0])
    return out


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
            _union(parent, i, j)

    clusters_by_root: dict[int, list[int]] = {}
    for idx in range(len(items)):
        root = _find(parent, idx)
        clusters_by_root.setdefault(root, []).append(idx)

    clusters = [sorted(indices) for indices in clusters_by_root.values()]
    return sorted(clusters, key=lambda component: (-len(component), component[0]))


def cluster_items_knn(
    items: Sequence[T],
    *,
    get_title: Callable[[T], str],
    get_text_chunks: Callable[[T], Iterable[str]],
    get_evidence_ids: Callable[[T], Sequence[str]] | None = None,
    embedder: Embedder | None = None,
    k: int = 10,
    overall_similarity_threshold: float = 0.78,
    require_mutual: bool = True,
    refine: bool = True,
    representative_similarity_threshold: float | None = 0.75,
    include_singletons: bool = True,
    brute_force_limit: int = 256,
) -> list[list[int]]:
    """Cluster functionally similar items via a k-nearest-neighbor graph.

    Notes
    -----
    The graph uses per-node top-k edges after threshold filtering. Exact duplicates always remain
    eligible regardless of threshold and k cutoffs. When refinement is enabled, members below the
    representative similarity threshold are split into singleton clusters.
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

    item_count = len(vectors)
    if item_count == 1:
        return [[0]] if include_singletons else []

    threshold = float(overall_similarity_threshold)
    top_k = max(0, int(k))
    use_bruteforce = item_count <= max(1, int(brute_force_limit))

    pair_indices: set[tuple[int, int]]
    if use_bruteforce:
        pair_indices = {
            (left, right)
            for left in range(item_count)
            for right in range(left + 1, item_count)
        }
    else:
        pair_indices = set(generate_candidate_pairs(vectors))

    pair_indices.update(_exact_duplicate_pairs([item.fingerprint for item in vectors]))

    pair_similarity_cache: dict[tuple[int, int], PairSimilarity] = {}
    neighbor_candidates: list[list[tuple[int, float, bool]]] = [[] for _ in range(item_count)]

    for left, right in sorted(pair_indices):
        similarity = compute_pair_similarity(vectors[left], vectors[right])
        pair_similarity_cache[(left, right)] = similarity

        if similarity.exact_duplicate or similarity.overall_similarity >= threshold:
            neighbor_candidates[left].append(
                (right, similarity.overall_similarity, similarity.exact_duplicate)
            )
            neighbor_candidates[right].append(
                (left, similarity.overall_similarity, similarity.exact_duplicate)
            )

    selected_neighbors: list[set[int]] = [set() for _ in range(item_count)]
    for idx, neighbors in enumerate(neighbor_candidates):
        best_by_neighbor: dict[int, tuple[float, bool]] = {}
        for neighbor_index, similarity_score, exact_duplicate in neighbors:
            previous = best_by_neighbor.get(neighbor_index)
            if previous is None:
                best_by_neighbor[neighbor_index] = (similarity_score, exact_duplicate)
                continue
            prev_score, prev_exact = previous
            if similarity_score > prev_score:
                best_by_neighbor[neighbor_index] = (similarity_score, exact_duplicate)
            elif similarity_score == prev_score and exact_duplicate and not prev_exact:
                best_by_neighbor[neighbor_index] = (similarity_score, exact_duplicate)

        exact_neighbors = {
            neighbor_index
            for neighbor_index, (_, exact_duplicate) in best_by_neighbor.items()
            if exact_duplicate
        }
        ranked_neighbors = sorted(
            (
                (neighbor_index, score)
                for neighbor_index, (score, exact_duplicate) in best_by_neighbor.items()
                if not exact_duplicate
            ),
            key=lambda item: (-item[1], item[0]),
        )

        keep = set(exact_neighbors)
        if top_k > 0:
            keep.update([neighbor_index for neighbor_index, _ in ranked_neighbors[:top_k]])
        selected_neighbors[idx] = keep

    graph_edges: set[tuple[int, int]] = set()
    for left, neighbor_set in enumerate(selected_neighbors):
        for right in neighbor_set:
            if left == right:
                continue
            if require_mutual and left not in selected_neighbors[right]:
                continue
            edge = (left, right) if left < right else (right, left)
            graph_edges.add(edge)

    components = _cluster_from_edges(item_count, sorted(graph_edges))

    if refine:
        refined_components: list[list[int]] = []
        threshold_rep = (
            None
            if representative_similarity_threshold is None
            else float(representative_similarity_threshold)
        )

        vector_values = [vector.vector for vector in vectors]
        for component in components:
            if len(component) == 1:
                if include_singletons:
                    refined_components.append(component)
                continue

            representative = _select_medoid_index(
                component,
                vectors=vector_values,
                pair_similarity_cache=pair_similarity_cache,
            )

            kept: list[int] = []
            removed: list[int] = []
            for member in component:
                if member == representative:
                    kept.append(member)
                    continue
                similarity_to_representative = _embedding_similarity(
                    representative,
                    member,
                    vectors=vector_values,
                    pair_similarity_cache=pair_similarity_cache,
                )
                if threshold_rep is None or similarity_to_representative >= threshold_rep:
                    kept.append(member)
                else:
                    removed.append(member)

            kept = sorted(set(kept))
            removed = sorted(set(removed))

            if kept and (len(kept) > 1 or include_singletons):
                refined_components.append(kept)
            if include_singletons:
                refined_components.extend([[idx] for idx in removed])

        components = refined_components
    elif not include_singletons:
        components = [component for component in components if len(component) > 1]

    components = [sorted(component) for component in components]
    components = [component for component in components if component]
    components.sort(key=lambda component: component[0])
    return components
