"""Deterministic pre-scoring for stage-2 backlog problem prioritization.

This module computes *pre-score signals* from evidence only. The pre-score is an input
to stage 2, not a final prioritization decision.

Design goals:
- Deterministic: the same inputs produce the same outputs.
- Human-readable: include a score breakdown and raw counts.
- Evidence-driven: use only stage-1 problem records + referenced evidence atoms.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backlog_core.operational_candidates import operational_candidate_receipt_errors

# These weights are heuristic and intentionally simple. Stage 2 may override
# them with explicit rationale; this module only provides pre-score signals.
_SEVERITY_SCORE: dict[str, float] = {"low": 0.20, "medium": 0.50, "high": 0.80, "blocker": 1.00}

# Evidence-source strength proxy (objective-ish sources score higher).
_SOURCE_STRENGTH: dict[str, float] = {
    # This source is runner-produced structured evidence.  The strength is used only
    # after ``operational_candidate_receipt_errors`` verifies the complete receipt;
    # an atom carrying this source label alone receives no typed-source bonus.
    "operational_failure_candidate": 1.00,
    "run_failure_event": 1.00,
    "report_validation_error": 0.90,
    "command_failure": 0.85,
    "token_monitoring_signal": 0.80,
    "token_monitoring_error": 0.75,
    "capability_warning_artifact": 0.70,
    "capability_notice_artifact": 0.60,
    "agent_stderr_artifact": 0.60,
    "agent_last_message_artifact": 0.55,
    "confusion_point": 0.70,
    "report_outcome": 0.90,
    "task_step_observation": 0.80,
    "task_attempt_observation": 0.80,
    "verification_observation": 0.85,
    "boundary_observation": 0.75,
    "batch_result_failure": 0.90,
    "suggested_change": 0.65,
    "confidence_missing": 0.60,
}


def _verified_operational_candidate_observation(
    atom: Mapping[str, Any],
) -> tuple[int, tuple[str, ...]] | None:
    """Return receipt-bound occurrence and source-run evidence, if trustworthy.

    Operational candidates deliberately use a synthetic atom/run identity, so treating
    that identity as one observed run loses the runner's recurrence ledger.  Conversely,
    reading the candidate's prose projection would let generated text affect ranking.
    This helper accepts only the full runner receipt after its content hashes and atom
    bindings validate, then cross-checks that its typed signals and parent bindings name
    the same unique source runs.
    """

    if _coerce_string(atom.get("source")) != "operational_failure_candidate":
        return None
    try:
        if operational_candidate_receipt_errors(atom):
            return None
    except (KeyError, TypeError, ValueError):
        # Malformed historical inputs are not ranking evidence.  Stage contracts retain
        # responsibility for reporting their integrity errors to operators.
        return None

    receipt_raw = atom.get("operational_candidate_receipt")
    if not isinstance(receipt_raw, Mapping):
        return None
    occurrence_count = receipt_raw.get("occurrence_count")
    signals_raw = receipt_raw.get("typed_signal_receipts")
    bindings_raw = receipt_raw.get("parent_bindings")
    if (
        isinstance(occurrence_count, bool)
        or not isinstance(occurrence_count, int)
        or occurrence_count <= 0
        or not isinstance(signals_raw, list)
        or not isinstance(bindings_raw, list)
    ):
        return None

    signal_run_ids = tuple(
        sorted(
            run_id
            for raw in signals_raw
            if isinstance(raw, Mapping)
            for run_id in [_coerce_string(raw.get("run_id"))]
            if run_id is not None
        )
    )
    binding_run_ids = tuple(
        sorted(
            run_id
            for raw in bindings_raw
            if isinstance(raw, Mapping)
            for run_id in [_coerce_string(raw.get("run_id"))]
            if run_id is not None
        )
    )
    if (
        len(signal_run_ids) != occurrence_count
        or len(set(signal_run_ids)) != occurrence_count
        or binding_run_ids != signal_run_ids
    ):
        return None
    return occurrence_count, signal_run_ids


def _coerce_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned:
            out.append(cleaned)
    return out


def _coerce_float_01(value: Any, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        try:
            return max(0.0, min(1.0, float(value.strip())))
        except ValueError:
            return default
    return default


def _severity_to_score(severity: str | None) -> float:
    if severity is None:
        return 0.50
    return _SEVERITY_SCORE.get(severity, 0.50)


def _impact_keyword_score(text: str | None) -> float:
    if not text:
        return 0.0
    lowered = text.lower()
    score = 0.0
    if any(
        term in lowered
        for term in (
            "blocked",
            "blocker",
            "block",
            "unable",
            "cannot",
            "can't",
            "fails",
            "failure",
            "crash",
        )
    ):
        score += 0.6
    if any(term in lowered for term in ("all runs", "every run", "majority", "systematic")):
        score += 0.4
    return max(0.0, min(1.0, score))


def compute_problem_priority_signals(
    problem_records: Sequence[dict[str, Any]],
    atoms: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute deterministic prioritization signals for stage-1 problem records.

    Parameters
    ----------
    problem_records:
        Stage-1 problem-record dicts. Each record should include ``problem_id``,
        ``severity``, ``confidence``, ``user_impact``, and ``evidence_atom_ids``.
    atoms:
        Eligible evidence atoms (output of ``backlog_core.extract_backlog_atoms``).

    Returns
    -------
    list[dict[str, Any]]
        One signal object per input record (in the same order), including:
        - ``problem_id`` (stable ID)
        - ``bucket_candidate`` (p0/p1/p2/p3/watch)
        - ``pre_score`` (float in [0, 1])
        - ``score_breakdown`` (mapping of component scores and raw counts)
    """
    atoms_by_id: dict[str, dict[str, Any]] = {}
    for atom in atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        if atom_id is None:
            continue
        atoms_by_id[atom_id] = atom

    results: list[dict[str, Any]] = []

    for rec in problem_records:
        pid = _coerce_string(rec.get("problem_id")) or "(missing problem_id)"
        severity = _coerce_string(rec.get("severity"))
        severity_score = _severity_to_score(severity)

        confidence = _coerce_float_01(rec.get("confidence"), default=0.5)
        impact_score = _impact_keyword_score(_coerce_string(rec.get("user_impact")))

        evidence_ids = _coerce_string_list(rec.get("evidence_atom_ids"))
        evidence_atoms = [atoms_by_id[eid] for eid in evidence_ids if eid in atoms_by_id]

        ordinary_evidence_atoms = [
            atom
            for atom in evidence_atoms
            if _coerce_string(atom.get("source")) != "operational_failure_candidate"
        ]
        verified_operational_observations: list[tuple[int, tuple[str, ...]]] = []
        unverified_operational_receipts = 0
        for atom in evidence_atoms:
            if _coerce_string(atom.get("source")) != "operational_failure_candidate":
                continue
            verified = _verified_operational_candidate_observation(atom)
            if verified is None:
                unverified_operational_receipts += 1
            else:
                verified_operational_observations.append(verified)

        runs = {
            _coerce_string(a.get("run_id"))
            for a in ordinary_evidence_atoms
            if _coerce_string(a.get("run_id")) is not None
        }
        operational_source_runs = {
            run_id
            for _occurrence_count, source_run_ids in verified_operational_observations
            for run_id in source_run_ids
        }
        runs.update(operational_source_runs)
        agents = {
            _coerce_string(a.get("agent"))
            for a in ordinary_evidence_atoms
            if _coerce_string(a.get("agent")) is not None
        }
        missions = {
            _coerce_string(a.get("mission_id"))
            for a in ordinary_evidence_atoms
            if _coerce_string(a.get("mission_id")) is not None
        }

        distinct_runs = len(runs)
        distinct_agents = len(agents)
        distinct_missions = len(missions)
        evidence_count = len(evidence_atoms)

        # Breadth is a coarse proxy for "more than one run observed this".
        runs_score = min(1.0, distinct_runs / 3.0)  # 1.0 at 3 distinct runs
        agents_score = min(1.0, distinct_agents / 2.0)  # 1.0 at 2 agents
        missions_score = min(1.0, distinct_missions / 2.0)  # 1.0 at 2 missions
        breadth_score = max(
            0.0,
            min(1.0, 0.55 * runs_score + 0.30 * agents_score + 0.15 * missions_score),
        )

        # Recurrence within observed evidence (independent of LLM confidence).  One
        # verified operational candidate may represent multiple exact runner-observed
        # occurrences.  Unverified candidate receipts contribute neither their synthetic
        # atom nor any prose-claimed count.
        verified_operational_occurrences = sum(
            occurrence_count
            for occurrence_count, _source_run_ids in verified_operational_observations
        )
        effective_observation_count = (
            len(ordinary_evidence_atoms) + verified_operational_occurrences
        )
        recurrence_score = min(1.0, effective_observation_count / 6.0)

        # Source strength: average of known per-source strengths.
        src_strengths: list[float] = []
        sources_count: dict[str, int] = {}
        for atom in evidence_atoms:
            source = _coerce_string(atom.get("source")) or "unknown"
            sources_count[source] = sources_count.get(source, 0) + 1
            if source == "operational_failure_candidate":
                if _verified_operational_candidate_observation(atom) is not None:
                    src_strengths.append(_SOURCE_STRENGTH[source])
                continue
            src_strengths.append(_SOURCE_STRENGTH.get(source, 0.50))
        source_strength_score = (sum(src_strengths) / len(src_strengths)) if src_strengths else 0.50

        # Trust combines model confidence (stage-1) and evidence-source strength.
        trust_score = max(0.0, min(1.0, 0.55 * confidence + 0.45 * source_strength_score))

        pre_score = max(
            0.0,
            min(
                1.0,
                0.26 * severity_score
                + 0.16 * impact_score
                + 0.20 * breadth_score
                + 0.14 * recurrence_score
                + 0.14 * trust_score
                + 0.10 * source_strength_score,
            ),
        )

        # Bucket thresholds are intentionally conservative. Severe + broad issues
        # should climb quickly; narrow low-confidence issues should fall to watch.
        if (
            severity_score >= 0.95
            and (breadth_score >= 0.45 or recurrence_score >= 0.60)
            and pre_score >= 0.75
        ):
            bucket = "p0"
        elif pre_score >= 0.60 or (
            severity_score >= 0.80 and source_strength_score >= 0.85 and trust_score >= 0.60
        ):
            bucket = "p1"
        elif pre_score >= 0.45:
            bucket = "p2"
        elif pre_score >= 0.30:
            bucket = "p3"
        else:
            bucket = "watch"

        score_breakdown: dict[str, Any] = {
            "severity": severity or "unknown",
            "severity_score": round(severity_score, 4),
            "confidence": round(confidence, 4),
            "impact_score": round(impact_score, 4),
            "breadth": {
                "distinct_runs": distinct_runs,
                "distinct_agents": distinct_agents,
                "distinct_missions": distinct_missions,
                "breadth_score": round(breadth_score, 4),
            },
            "recurrence": {
                "evidence_atoms_cited": evidence_count,
                "effective_observations": effective_observation_count,
                "recurrence_score": round(recurrence_score, 4),
            },
            "operational_candidates": {
                "verified_receipts": len(verified_operational_observations),
                "unverified_receipts": unverified_operational_receipts,
                "verified_occurrences": verified_operational_occurrences,
                "distinct_source_runs": len(operational_source_runs),
            },
            "sources": {
                "source_strength_score": round(source_strength_score, 4),
                "source_counts": dict(sorted(sources_count.items())),
            },
            "trust_score": round(trust_score, 4),
            "pre_score": round(pre_score, 4),
            "bucket_candidate": bucket,
        }

        results.append(
            {
                "problem_id": pid,
                "bucket_candidate": bucket,
                "pre_score": pre_score,
                "score_breakdown": score_breakdown,
            }
        )

    return results
