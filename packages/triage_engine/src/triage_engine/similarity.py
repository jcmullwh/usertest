from __future__ import annotations

import hashlib
import json
import os
import random
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import TypeVar

from triage_engine.embeddings import (
    Embedder,
    dot,
    get_default_embedder,
    l2_normalize,
)
from triage_engine.text import extract_path_anchors_from_chunks, tokenize

T = TypeVar("T")

__all__ = [
    "ItemVector",
    "PairSimilarity",
    "build_item_vectors",
    "compute_pair_similarity",
    "get_similarity_weights",
    "generate_candidate_pairs",
]


_WHITESPACE_RE = re.compile(r"\s+")


def _canonical_text(text: str, *, max_chars: int = 64_000) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", text.strip())
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _join_chunks(chunks: Sequence[str], *, max_chars: int) -> str:
    joined = "\n".join(chunk for chunk in chunks if chunk)
    if len(joined) <= max_chars:
        return joined

    # Keep head + tail to retain both context and any final error messages.
    head = joined[: max_chars // 2]
    tail = joined[-(max_chars - len(head)) :]
    return head + "\n...[snip]...\n" + tail


def _expand_path_anchors(anchors: Iterable[str]) -> frozenset[str]:
    expanded: set[str] = set()
    for raw in anchors:
        anchor = raw.strip().lower().replace("\\", "/")
        if not anchor:
            continue
        expanded.add(anchor)
        parts = [part for part in anchor.split("/") if part]
        if not parts:
            continue
        expanded.add(parts[-1])
        if len(parts) >= 2:
            expanded.add("/".join(parts[-2:]))
    return frozenset(expanded)


@dataclass(frozen=True)
class ItemVector:
    """One item represented as an embedding vector + high-precision metadata."""

    title: str
    text: str
    title_tokens: frozenset[str]
    anchors: frozenset[str]
    evidence_ids: frozenset[str]
    fingerprint: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class PairSimilarity:
    """Similarity breakdown between two items."""

    embedding_cosine: float
    embedding_similarity: float
    anchor_jaccard: float
    title_jaccard: float
    evidence_overlap: int
    exact_duplicate: bool
    overall_similarity: float




@dataclass(frozen=True)
class SimilarityWeights:
    """Weights used by :func:`compute_pair_similarity`.

    You can override these at runtime via environment variables:

    - TRIAGE_ENGINE_SIM_WEIGHTS:
        Either a JSON object like {"embedding": 0.8, "title": 0.1, ...}
        or a comma-separated list "0.8,0.1,0.06,0.04" in the order:
        embedding,title,anchor,evidence.
    - TRIAGE_ENGINE_SIM_WEIGHT_EMBEDDING
    - TRIAGE_ENGINE_SIM_WEIGHT_TITLE
    - TRIAGE_ENGINE_SIM_WEIGHT_ANCHOR
    - TRIAGE_ENGINE_SIM_WEIGHT_EVIDENCE

    Weights are normalized to sum to 1.0 when possible.
    """

    embedding: float = 0.82
    title: float = 0.10
    anchor: float = 0.06
    evidence: float = 0.02

    def normalized(self) -> SimilarityWeights:
        total = float(self.embedding + self.title + self.anchor + self.evidence)
        if total <= 0.0:
            return self
        return SimilarityWeights(
            embedding=self.embedding / total,
            title=self.title / total,
            anchor=self.anchor / total,
            evidence=self.evidence / total,
        )


def _parse_float_env(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def get_similarity_weights() -> SimilarityWeights:
    """Return similarity weights, potentially overridden by environment."""

    weights = SimilarityWeights()

    raw = os.getenv("TRIAGE_ENGINE_SIM_WEIGHTS")
    if raw:
        raw = raw.strip()
        parsed: dict[str, float] | None = None
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    parsed = {
                        str(k): float(v)
                        for k, v in obj.items()
                        if k in {"embedding", "title", "anchor", "evidence"}
                    }
            except Exception:
                parsed = None
        else:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            if len(parts) == 4:
                try:
                    parsed = {
                        "embedding": float(parts[0]),
                        "title": float(parts[1]),
                        "anchor": float(parts[2]),
                        "evidence": float(parts[3]),
                    }
                except ValueError:
                    parsed = None

        if parsed:
            weights = SimilarityWeights(
                embedding=parsed.get("embedding", weights.embedding),
                title=parsed.get("title", weights.title),
                anchor=parsed.get("anchor", weights.anchor),
                evidence=parsed.get("evidence", weights.evidence),
            )

    # Per-field overrides win.
    emb = _parse_float_env("TRIAGE_ENGINE_SIM_WEIGHT_EMBEDDING")
    title = _parse_float_env("TRIAGE_ENGINE_SIM_WEIGHT_TITLE")
    anchor = _parse_float_env("TRIAGE_ENGINE_SIM_WEIGHT_ANCHOR")
    evidence = _parse_float_env("TRIAGE_ENGINE_SIM_WEIGHT_EVIDENCE")

    if emb is not None or title is not None or anchor is not None or evidence is not None:
        weights = SimilarityWeights(
            embedding=weights.embedding if emb is None else emb,
            title=weights.title if title is None else title,
            anchor=weights.anchor if anchor is None else anchor,
            evidence=weights.evidence if evidence is None else evidence,
        )

    return weights.normalized()

def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter <= 0:
        return 0.0
    union = len(a | b)
    return inter / float(union) if union else 0.0


def compute_pair_similarity(left: ItemVector, right: ItemVector) -> PairSimilarity:
    """Compute a composite similarity score for two embedded items."""

    exact = bool(left.fingerprint and left.fingerprint == right.fingerprint)

    # Vectors are L2-normalized; cosine reduces to dot product.
    cos = dot(left.vector, right.vector)
    cos = max(-1.0, min(1.0, cos))

    # Normalize cosine into [0, 1] for easier composition.
    emb_sim = (cos + 1.0) / 2.0

    anchor_sim = _jaccard(left.anchors, right.anchors)
    title_sim = _jaccard(left.title_tokens, right.title_tokens)
    evidence_overlap = len(left.evidence_ids & right.evidence_ids)
    evidence_signal = 0.0 if evidence_overlap <= 0 else min(1.0, evidence_overlap / 2.0)

    if exact:
        overall = 1.0
    else:
        # Titles are treated as an auxiliary signal (useful for very short items).
        w = get_similarity_weights()
        overall = (
            w.embedding * emb_sim
            + w.title * title_sim
            + w.anchor * anchor_sim
            + w.evidence * evidence_signal
        )
        overall = max(0.0, min(1.0, overall))

    return PairSimilarity(
        embedding_cosine=cos,
        embedding_similarity=emb_sim,
        anchor_jaccard=anchor_sim,
        title_jaccard=title_sim,
        evidence_overlap=evidence_overlap,
        exact_duplicate=exact,
        overall_similarity=overall,
    )


class _SparseRandomHyperplaneLSH:
    """LSH signatures for cosine similarity using sparse random hyperplanes."""

    def __init__(
        self,
        dim: int,
        *,
        n_bits: int = 128,
        indices_per_bit: int = 32,
        seed: int = 1337,
    ) -> None:
        if dim <= 0:
            raise ValueError("LSH dim must be > 0")
        if n_bits <= 0:
            raise ValueError("LSH n_bits must be > 0")
        if indices_per_bit <= 0:
            raise ValueError("LSH indices_per_bit must be > 0")

        rng = random.Random(seed)
        self._dim = dim
        self._n_bits = n_bits
        self._indices: list[list[int]] = []
        self._signs: list[list[float]] = []

        for _ in range(n_bits):
            indices: list[int] = []
            signs: list[float] = []
            for _ in range(indices_per_bit):
                indices.append(rng.randrange(dim))
                signs.append(1.0 if rng.random() < 0.5 else -1.0)
            self._indices.append(indices)
            self._signs.append(signs)

    @property
    def n_bits(self) -> int:
        return self._n_bits

    def signature(self, vec: Sequence[float]) -> int:
        if len(vec) != self._dim:
            raise ValueError("Vector length does not match LSH dimension")

        sig = 0
        for bit in range(self._n_bits):
            acc = 0.0
            indices = self._indices[bit]
            signs = self._signs[bit]
            for idx, sign in zip(indices, signs, strict=True):
                acc += sign * vec[idx]
            if acc >= 0.0:
                sig |= 1 << bit
        return sig


def build_item_vectors(
    items: Sequence[T],
    *,
    get_title: Callable[[T], str],
    get_text_chunks: Callable[[T], Iterable[str]],
    get_evidence_ids: Callable[[T], Sequence[str]] | None = None,
    embedder: Embedder | None = None,
    max_text_chars: int = 12_000,
) -> list[ItemVector]:
    """Build embedded vectors for arbitrary items.

    The engine operates on *text chunks* supplied by the caller. Titles are treated as
    one chunk among many.
    """

    if not items:
        return []

    title_list: list[str] = []
    title_tokens_list: list[frozenset[str]] = []
    text_list: list[str] = []
    anchors_list: list[frozenset[str]] = []
    evidence_list: list[frozenset[str]] = []
    fingerprints: list[str] = []

    for item in items:
        title_raw = get_title(item)
        title = title_raw if isinstance(title_raw, str) else str(title_raw)
        title_tokens = frozenset(tokenize(title))

        chunks_raw = [chunk for chunk in get_text_chunks(item) if isinstance(chunk, str)]
        chunks = [chunk.strip() for chunk in chunks_raw if chunk and chunk.strip()]

        # Ensure title is included as a chunk (some callers may pass only body chunks).
        if title and title not in chunks:
            chunks.insert(0, title)

        text = _join_chunks(chunks, max_chars=int(max_text_chars))
        canonical = _canonical_text(text)
        fingerprint = _sha256_hex(canonical) if canonical else ""

        anchors = _expand_path_anchors(extract_path_anchors_from_chunks(chunks))

        evidence_ids: frozenset[str]
        if get_evidence_ids is None:
            evidence_ids = frozenset()
        else:
            raw_ids = get_evidence_ids(item)
            evidence_ids = frozenset(
                {
                    value.strip()
                    for value in raw_ids
                    if isinstance(value, str) and value.strip()
                }
            )

        title_list.append(title)
        title_tokens_list.append(title_tokens)
        text_list.append(text)
        anchors_list.append(anchors)
        evidence_list.append(evidence_ids)
        fingerprints.append(fingerprint)

    chosen = embedder or get_default_embedder()
    vectors_raw = chosen.embed_texts(text_list)
    if len(vectors_raw) != len(items):
        raise ValueError(
            "Embedder returned unexpected vector count: "
            f"expected {len(items)}, got {len(vectors_raw)}"
        )

    vectors = [l2_normalize(vec) for vec in vectors_raw]

    out: list[ItemVector] = []
    for title, title_tokens, text, anchors, evidence_ids, fingerprint, vec in zip(
        title_list,
        title_tokens_list,
        text_list,
        anchors_list,
        evidence_list,
        fingerprints,
        vectors,
        strict=True,
    ):
        out.append(
            ItemVector(
                title=title,
                text=text,
                title_tokens=title_tokens,
                anchors=anchors,
                evidence_ids=evidence_ids,
                fingerprint=fingerprint,
                vector=vec,
            )
        )
    return out


def generate_candidate_pairs(
    items: Sequence[ItemVector],
    *,
    max_bucket_size: int = 64,
    sim_bands: int = 8,
    sim_band_bits: int = 16,
    lsh_bits: int = 128,
    lsh_indices_per_bit: int = 32,
    seed: int = 1337,
    max_anchors_per_item: int = 8,
    max_title_tokens_per_item: int = 6,
) -> set[tuple[int, int]]:
    """Generate candidate index pairs using LSH + exact buckets."""

    n = len(items)
    if n <= 1:
        return set()

    if n <= 64:
        return {(i, j) for i in range(n) for j in range(i + 1, n)}

    buckets: dict[tuple[object, ...], list[int]] = {}

    def _add(key: tuple[object, ...], idx: int) -> None:
        buckets.setdefault(key, []).append(idx)

    # Exact duplicate bucket.
    for idx, item in enumerate(items):
        if item.fingerprint:
            _add(("f", item.fingerprint), idx)

    # Evidence + anchor + title-token buckets.
    for idx, item in enumerate(items):
        for ev in sorted(item.evidence_ids):
            _add(("e", ev), idx)

        anchors = sorted(item.anchors)
        for anchor in anchors[: max(0, int(max_anchors_per_item))]:
            _add(("a", anchor), idx)

        # Title tokens help avoid missing obvious duplicates when bodies are short.
        tokens = sorted(item.title_tokens)
        for token in tokens[: max(0, int(max_title_tokens_per_item))]:
            _add(("t", token), idx)

    # LSH on embeddings.
    dim = len(items[0].vector)
    lsh = _SparseRandomHyperplaneLSH(
        dim,
        n_bits=int(lsh_bits),
        indices_per_bit=int(lsh_indices_per_bit),
        seed=int(seed),
    )

    signatures = [lsh.signature(item.vector) for item in items]

    bands = max(0, int(sim_bands))
    band_bits = max(0, int(sim_band_bits))
    if bands and band_bits:
        mask = (1 << band_bits) - 1
        for idx, sig in enumerate(signatures):
            for band in range(bands):
                shift = band * band_bits
                band_value = (sig >> shift) & mask
                _add(("s", band, band_value), idx)

    pairs: set[tuple[int, int]] = set()
    for indices in buckets.values():
        if len(indices) < 2:
            continue
        if len(indices) > int(max_bucket_size):
            continue

        uniq = sorted(set(indices))
        for i, j in combinations(uniq, 2):
            pairs.add((i, j))

    return pairs
