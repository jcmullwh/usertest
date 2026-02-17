from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TrustEvidence:
    """Evidence record used for trust assessment.

    The engine is intentionally generic: callers can map their domain evidence (usertest atoms,
    customer feedback, PR comments, log snippets) into this minimal schema.
    """

    # Stable ID for this evidence object (optional; used only for debugging).
    evidence_id: str | None = None

    # Group identifier representing an "independent" source of evidence (e.g., run_id, session_id).
    group: str | None = None

    # Source identifier representing the producer (e.g., model name, author id).
    source: str | None = None

    # Domain-specific kind (e.g., "run_failure_event", "confusion_point").
    kind: str | None = None

    # Relative weight of this evidence (0.0 .. 1.0+). Defaults to 1.0.
    weight: float = 1.0


@dataclass(frozen=True)
class TrustAssessment:
    """Trust assessment result."""

    score: float
    level: str
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def assess_trust(
    evidence: list[TrustEvidence],
    *,
    confidence: float | None = None,
) -> TrustAssessment:
    """Assess trustworthiness for a claim backed by evidence.

    The returned score is *not* a probability; it is a bounded heuristic intended for ranking and
    prioritization.

    Signals
    -------
    - corroboration: how many independent groups cite the claim
    - diversity: how many distinct sources cite the claim
    - weight: cumulative weight of evidence
    - confidence: optional caller-provided model confidence
    """

    if confidence is not None:
        confidence = _clamp01(float(confidence))

    total_weight = 0.0
    groups: set[str] = set()
    sources: set[str] = set()
    kinds: dict[str, int] = {}

    for item in evidence:
        weight = float(item.weight)
        if math.isnan(weight) or weight <= 0.0:
            weight = 0.0
        total_weight += weight
        if item.group:
            groups.add(item.group)
        if item.source:
            sources.add(item.source)
        if item.kind:
            kinds[item.kind] = kinds.get(item.kind, 0) + 1

    # If callers didn't provide a grouping key, fall back to per-evidence granularity.
    group_count = len(groups) if groups else len(evidence)
    source_count = len(sources)

    # Saturating transforms.
    corroboration = 1.0 - math.exp(-0.75 * float(group_count))
    diversity = 1.0 - math.exp(-0.55 * float(source_count)) if source_count > 0 else 0.0
    weight_signal = 1.0 - math.exp(-0.18 * total_weight)

    # Base signal: prioritize corroboration.
    base = 0.58 * corroboration + 0.22 * weight_signal + 0.20 * diversity

    score = base
    if confidence is not None:
        score = 0.78 * base + 0.22 * confidence
    score = _clamp01(score)

    level = "low"
    if score >= 0.75:
        level = "high"
    elif score >= 0.45:
        level = "medium"

    return TrustAssessment(
        score=score,
        level=level,
        signals={
            "evidence_count": len(evidence),
            "group_count": group_count,
            "source_count": source_count,
            "total_weight": total_weight,
            "kinds": dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))),
            "corroboration": corroboration,
            "diversity": diversity,
            "weight_signal": weight_signal,
            "confidence": confidence,
        },
    )
