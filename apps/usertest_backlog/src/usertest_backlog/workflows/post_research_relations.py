"""Runner-owned same-mechanism consolidation between research and optioning."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from typing import Any

from backlog_core.case_lineage import verified_mechanism_identities_from_case_registry
from backlog_miner.research_evidence import verify_persisted_research_evidence


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if _text(item) is not None))


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _unique_sorted_records(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_json = {json.dumps(item, sort_keys=True, separators=(",", ":")): item for item in value}
    return [by_json[key] for key in sorted(by_json)]


def _argument_identity(value: Any) -> dict[str, Any] | None:
    argument = value if isinstance(value, dict) else None
    if argument is None:
        return None
    return {
        key: argument.get(key) for key in ("slot", "ast_sha256") if argument.get(key) is not None
    }


def _controlled_input_identity(value: Any) -> dict[str, Any] | None:
    controlled = value if isinstance(value, dict) else None
    if controlled is None:
        return None
    difference_raw = controlled.get("difference")
    difference = difference_raw if isinstance(difference_raw, dict) else None
    if difference is None:
        return None
    projected_difference = {
        key: difference.get(key)
        for key in (
            "mechanism_symbol",
            "slot",
            "difference_kind",
            "baseline_argument",
            "challenge_argument",
            "baseline_file_sha256",
            "challenge_file_sha256",
            "content_relation",
        )
        if difference.get(key) is not None
    }
    for field in ("support_argument", "control_argument"):
        argument = _argument_identity(difference.get(field))
        if argument is not None:
            projected_difference[field] = argument
    if not projected_difference.get("slot"):
        return None
    return {
        "difference_count": controlled.get("difference_count"),
        "difference": projected_difference,
    }


def _observation_identity(value: Any) -> dict[str, Any] | None:
    observation = value if isinstance(value, dict) else None
    if observation is None:
        return None
    projection: dict[str, Any] = {
        key: observation.get(key)
        for key in (
            "polarity",
            "source",
            "difference_kind",
            "exit_code",
        )
        if observation.get(key) is not None
    }
    for field in ("baseline", "challenge", "support", "control"):
        raw_side = observation.get(field)
        if not isinstance(raw_side, dict):
            continue
        projection[field] = {
            key: raw_side.get(key) for key in ("exit_code",) if raw_side.get(key) is not None
        }
    assertion = observation.get("assertion")
    if isinstance(assertion, dict):
        projection["assertion"] = {
            key: assertion.get(key)
            for key in ("source", "operator", "expected")
            if assertion.get(key) is not None
        }
    return projection or None


def _control_point_identity(value: Any) -> dict[str, Any] | None:
    point = value if isinstance(value, dict) else None
    if point is None:
        return None
    projection = {
        key: point.get(key)
        for key in (
            "mechanism_symbol",
            "slot",
            "path",
            "code_path",
        )
        if point.get(key) is not None
    }
    symbols = sorted(_strings(point.get("mechanism_symbols")))
    if symbols:
        projection["mechanism_symbols"] = symbols
    return projection or None


def _code_path_identity(value: Any) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        path = {
            key: raw.get(key)
            for key in (
                "symbol",
                "path",
            )
            if raw.get(key) is not None
        }
        if path:
            paths.append(path)
    return sorted(paths, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def verified_causal_evidence_projection(
    dossier: dict[str, Any],
    *,
    verified_mechanism_sha256: str,
) -> dict[str, Any] | None:
    """Return cross-case identity from runner-attested causal facts.

    Model relationship wording and case-local hypothesis/experiment IDs remain in the
    retained research receipt for audit, but do not make two independent proofs of the
    same mechanism look different.
    """

    verification_raw = dossier.get("evidence_verification")
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    provenance_raw = verification.get("verified_mechanism_provenance")
    provenance = provenance_raw if isinstance(provenance_raw, dict) else None
    if provenance is None or verification.get(
        "verified_mechanism_provenance_sha256"
    ) != _canonical_sha256(provenance):
        return None
    hypothesis_id = _text(provenance.get("primary_hypothesis_id"))
    if hypothesis_id is None:
        return None

    interventions: list[dict[str, Any]] = []
    for field in ("control_verifications", "falsification_interventions"):
        raw = verification.get(field)
        for receipt in raw if isinstance(raw, list) else []:
            if not isinstance(receipt, dict) or receipt.get("hypothesis_id") != hypothesis_id:
                continue
            controlled = receipt.get("controlled_input_difference")
            if not isinstance(controlled, dict):
                continue
            interventions.append(
                {
                    "mechanism_symbols": sorted(_strings(receipt.get("mechanism_symbols"))),
                    "controlled_input_difference": _controlled_input_identity(controlled),
                    "observed_polarity": _observation_identity(receipt.get("observed_polarity")),
                    "observable_difference": _observation_identity(
                        receipt.get("observable_difference")
                    ),
                }
            )

    closures: list[dict[str, Any]] = []
    raw_closures = verification.get("deterministic_mechanism_closures")
    for closure in raw_closures if isinstance(raw_closures, list) else []:
        if not isinstance(closure, dict) or closure.get("hypothesis_id") != hypothesis_id:
            continue
        closures.append(
            {
                "scenario_kind": closure.get("scenario_kind"),
                "closure_basis": closure.get("closure_basis"),
                "mechanism_symbols": sorted(_strings(closure.get("mechanism_symbols"))),
                "code_path": _code_path_identity(closure.get("code_path")),
                "observed_result": _observation_identity(closure.get("observed_result")),
            }
        )
    control_points = provenance.get("research_probe_control_points")
    control_points = control_points if isinstance(control_points, list) else []
    projected_control_points = [
        projected
        for value in control_points
        for projected in [_control_point_identity(value)]
        if projected is not None
    ]
    if not interventions and not closures and not projected_control_points:
        return None
    return {
        "schema_version": 2,
        "repo_revision": _text(dossier.get("repo_revision")),
        "verified_mechanism_sha256": verified_mechanism_sha256,
        "research_probe_control_points": sorted(
            _unique_sorted_records(projected_control_points),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
        "interventions": sorted(
            _unique_sorted_records(interventions),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
        "deterministic_closures": sorted(
            _unique_sorted_records(closures),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
    }


def _verified_causal_signature(
    dossier: dict[str, Any],
    *,
    verified_mechanism_sha256: str,
) -> str | None:
    projection = verified_causal_evidence_projection(
        dossier,
        verified_mechanism_sha256=verified_mechanism_sha256,
    )
    return _canonical_sha256(projection) if projection is not None else None


def collapse_post_research_verified_mechanisms(
    *,
    problem_records: list[dict[str, Any]],
    priority_decisions: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    case_registry: dict[str, Any],
    verify_dossier: Callable[[dict[str, Any]], tuple[bool, list[str]]] = (
        verify_persisted_research_evidence
    ),
) -> dict[str, Any]:
    """Collapse only dossiers with the same runner-verified mechanism and revision.

    Every original symptom and outcome oracle remains on the canonical problem packet.
    The original stage-3 dossiers remain persisted separately; downstream stages receive
    one representative dossier so they produce one option/plan unit for one mechanism.
    """

    identities = verified_mechanism_identities_from_case_registry(case_registry)
    eligible: dict[str, dict[str, Any]] = {}
    causal_signatures: dict[str, str] = {}
    for dossier in research_dossiers:
        case_id = _text(dossier.get("case_id"))
        verification = dossier.get("evidence_verification")
        receipt = verification if isinstance(verification, dict) else {}
        identity = identities.get(case_id or "")
        ready, _errors = verify_dossier(dossier)
        causal_signature = (
            _verified_causal_signature(
                dossier,
                verified_mechanism_sha256=identity,
            )
            if identity is not None
            else None
        )
        if (
            case_id is not None
            and ready
            and identity is not None
            and causal_signature is not None
            and receipt.get("status") == "verified"
            and receipt.get("verified_mechanism_sha256") == identity
            and _text(dossier.get("repo_revision")) is not None
        ):
            eligible[case_id] = dossier
            causal_signatures[case_id] = causal_signature

    grouped: dict[tuple[str, str], list[str]] = {}
    for case_id, dossier in eligible.items():
        grouped.setdefault(
            (
                causal_signatures[case_id],
                str(dossier["repo_revision"]),
            ),
            [],
        ).append(case_id)
    groups = [
        (identity, revision, sorted(case_ids))
        for (identity, revision), case_ids in sorted(grouped.items())
        if len(case_ids) > 1
    ]
    if not groups:
        return {
            "problem_records": [dict(item) for item in problem_records],
            "priority_decisions": [dict(item) for item in priority_decisions],
            "research_dossiers": [dict(item) for item in research_dossiers],
            "groups": [],
            "case_aliases": {},
        }

    problems_by_case = {
        str(item.get("case_id")): item
        for item in problem_records
        if _text(item.get("case_id")) is not None
    }
    priorities_by_case = {
        str(item.get("case_id")): item
        for item in priority_decisions
        if _text(item.get("case_id")) is not None
    }
    dossiers_by_case = {
        str(item.get("case_id")): item
        for item in research_dossiers
        if _text(item.get("case_id")) is not None
    }
    canonical_by_case: dict[str, str] = {}
    group_meta: list[dict[str, Any]] = []
    canonical_problem_overrides: dict[str, dict[str, Any]] = {}
    canonical_priority_overrides: dict[str, dict[str, Any]] = {}
    canonical_dossier_overrides: dict[str, dict[str, Any]] = {}
    bucket_rank = {"p0": 0, "p1": 1, "p2": 2, "watch": 3}
    registry_cases_raw = case_registry.get("cases")
    registry_cases = registry_cases_raw if isinstance(registry_cases_raw, dict) else {}

    def _canonical_rank(case_id: str) -> tuple[int, int, int, str]:
        entry_raw = registry_cases.get(case_id)
        entry = entry_raw if isinstance(entry_raw, dict) else {}
        lifecycle = int(isinstance(entry.get("current_lifecycle"), dict))
        durable_records = sum(
            len(value) if isinstance(value, (dict, list)) else 0
            for value in (
                entry.get("plan_revisions"),
                entry.get("ticket_records"),
                entry.get("plan_outcomes"),
            )
        )
        revision_raw = entry.get("case_revision")
        revision = revision_raw if isinstance(revision_raw, int) else 0
        return (-lifecycle, -durable_records, -revision, case_id)

    for causal_signature, revision, case_ids in groups:
        if any(case_id not in problems_by_case for case_id in case_ids):
            continue
        canonical_case_id = min(case_ids, key=_canonical_rank)
        case_ids = [canonical_case_id, *sorted(set(case_ids) - {canonical_case_id})]
        absorbed_case_ids = case_ids[1:]
        for case_id in absorbed_case_ids:
            canonical_by_case[case_id] = canonical_case_id
        problem_group = [problems_by_case[case_id] for case_id in case_ids]
        dossier_group = [dossiers_by_case[case_id] for case_id in case_ids]
        canonical = deepcopy(problems_by_case[canonical_case_id])
        canonical_problem_id = str(canonical["problem_id"])
        facets = [
            {
                "case_id": item.get("case_id"),
                "problem_id": item.get("problem_id"),
                "title": item.get("title"),
                "problem": item.get("problem"),
                "user_impact": item.get("user_impact"),
                "canonical_symptoms": _strings(item.get("canonical_symptoms")),
                "evidence_atom_ids": _strings(item.get("evidence_atom_ids")),
            }
            for item in problem_group
        ]
        outcome_oracles = [
            deepcopy(oracle)
            for dossier in dossier_group
            for verification in [dossier.get("evidence_verification")]
            if isinstance(verification, dict)
            for oracle in (
                verification.get("outcome_oracles")
                if isinstance(verification.get("outcome_oracles"), list)
                else []
            )
            if isinstance(oracle, dict)
        ]
        canonical["case_member_problem_ids"] = list(
            dict.fromkeys(
                problem_id
                for item in problem_group
                for problem_id in [
                    str(item.get("problem_id")),
                    *_strings(item.get("case_member_problem_ids")),
                ]
                if problem_id
            )
        )
        for field in (
            "evidence_atom_ids",
            "source_evidence_atom_ids",
            "derived_evidence_atom_ids",
            "canonical_symptoms",
            "related_case_ids",
        ):
            canonical[field] = list(
                dict.fromkeys(
                    value for item in problem_group for value in _strings(item.get(field))
                )
            )
        canonical["symptom_facets"] = facets
        canonical["same_mechanism_outcome_oracles"] = outcome_oracles
        mechanism_identity = identities[canonical_case_id]
        canonical["verified_mechanism_sha256"] = mechanism_identity
        canonical["verified_causal_signature_sha256"] = causal_signature
        canonical["same_cause_group_id"] = f"cause:{causal_signature[:16]}"
        canonical["absorbed_case_ids"] = absorbed_case_ids
        canonical["case_relation_actions"] = [
            {
                "action": "same_cause_group",
                "group_id": canonical["same_cause_group_id"],
                "rationale": (
                    "Runner research established the same content-addressed code surface "
                    "and causal intervention or deterministic closure at the same revision."
                ),
                "review_confidence": 1.0,
                "source": "runner_verified_causal_signature_v1",
            }
        ]
        canonical_problem_overrides[canonical_case_id] = canonical

        priority_group = [
            priorities_by_case[case_id] for case_id in case_ids if case_id in priorities_by_case
        ]
        if priority_group:
            priority = deepcopy(priorities_by_case.get(canonical_case_id, priority_group[0]))
            priority["case_id"] = canonical_case_id
            priority["problem_id"] = canonical_problem_id
            priority["selected_for_research"] = True
            priority["priority_status"] = "prioritized"
            priority["priority_bucket"] = min(
                (_text(item.get("priority_bucket")) or "watch" for item in priority_group),
                key=lambda value: bucket_rank.get(value, 99),
            )
            priority["same_mechanism_priority_facets"] = [
                {
                    "case_id": item.get("case_id"),
                    "problem_id": item.get("problem_id"),
                    "priority_bucket": item.get("priority_bucket"),
                    "priority_rationale": item.get("priority_rationale"),
                }
                for item in priority_group
            ]
            canonical_priority_overrides[canonical_case_id] = priority

        dossier = deepcopy(dossiers_by_case[canonical_case_id])
        proof_refs = [
            {
                "case_id": item.get("case_id"),
                "problem_id": item.get("problem_id"),
                "repo_revision": item.get("repo_revision"),
                "evidence_verification_receipt_sha256": (
                    item.get("evidence_verification", {}).get("receipt_sha256")
                    if isinstance(item.get("evidence_verification"), dict)
                    else None
                ),
            }
            for item in dossier_group
        ]
        bundle = {
            "schema_version": 1,
            "canonical_case_id": canonical_case_id,
            "canonical_problem_id": canonical_problem_id,
            "verified_mechanism_sha256": mechanism_identity,
            "verified_causal_signature_sha256": causal_signature,
            "repo_revision": revision,
            "member_case_ids": case_ids,
            "member_problem_ids": canonical["case_member_problem_ids"],
            "research_proof_refs": proof_refs,
            "outcome_oracle_ids": sorted(
                str(oracle.get("outcome_oracle_id"))
                for oracle in outcome_oracles
                if _text(oracle.get("outcome_oracle_id")) is not None
            ),
            "member_research_dossiers": [deepcopy(item) for item in dossier_group],
        }
        bundle["bundle_sha256"] = _canonical_sha256(bundle)
        dossier["post_research_same_mechanism_bundle"] = bundle
        canonical_dossier_overrides[canonical_case_id] = dossier
        group_meta.append(
            {
                key: deepcopy(value)
                for key, value in bundle.items()
                if key != "member_research_dossiers"
            }
        )

    def _collapse(
        items: list[dict[str, Any]], overrides: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        emitted: set[str] = set()
        for item in items:
            case_id = _text(item.get("case_id"))
            canonical_case_id = canonical_by_case.get(case_id or "", case_id)
            if canonical_case_id is None or canonical_case_id in emitted:
                continue
            emitted.add(canonical_case_id)
            result.append(deepcopy(overrides.get(canonical_case_id, item)))
        return result

    return {
        "problem_records": _collapse(problem_records, canonical_problem_overrides),
        "priority_decisions": _collapse(priority_decisions, canonical_priority_overrides),
        "research_dossiers": _collapse(research_dossiers, canonical_dossier_overrides),
        "groups": group_meta,
        "case_aliases": canonical_by_case,
    }


__all__ = [
    "collapse_post_research_verified_mechanisms",
    "verified_causal_evidence_projection",
]
