"""Open registry for runner-minted positive-outcome semantic bases."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from backlog_core.causal_proof import (
    canonical_json_sha256,
    command_authorization_errors,
    command_authorization_identity,
    content_bound_payload,
)


@dataclass(frozen=True)
class PositiveBasisContext:
    semantic_claim: Mapping[str, Any]
    predicate: Mapping[str, Any]
    source_atom_ids: frozenset[str]
    evidence_assignment: Mapping[str, Any]
    experiments: Mapping[str, Mapping[str, Any]] | None = None
    clean_replays: Mapping[str, Mapping[str, Any]] | None = None
    planning_workspace: Any = None
    artifact_receipts: Sequence[Mapping[str, Any]] = ()
    symbol_receipts: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class PositiveBasisResult:
    basis: dict[str, Any] | None = None
    diagnostics: tuple[str, ...] = ()


class PositiveBasisAdapter(Protocol):
    basis_kind: str
    adapter_version: str

    def bind(self, context: PositiveBasisContext) -> PositiveBasisResult: ...


class PositiveBasisRegistry:
    def __init__(self, adapters: Iterable[PositiveBasisAdapter] = ()) -> None:
        self._adapters: dict[str, PositiveBasisAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: PositiveBasisAdapter) -> None:
        kind = str(getattr(adapter, "basis_kind", "")).strip()
        version = str(getattr(adapter, "adapter_version", "")).strip()
        if not kind or not version:
            raise ValueError("positive_basis_adapter_identity_invalid")
        if kind in self._adapters:
            raise ValueError(f"positive_basis_adapter_duplicate:{kind}")
        self._adapters[kind] = adapter

    def evaluate(self, context: PositiveBasisContext) -> PositiveBasisResult:
        kind = context.semantic_claim.get("kind")
        adapter = self._adapters.get(str(kind))
        if adapter is None:
            return PositiveBasisResult(
                diagnostics=(f"positive_basis_adapter_unavailable:{kind}",)
            )
        return adapter.bind(context)


def _field_value(document: Any, field_path: str) -> tuple[bool, Any]:
    if field_path == "$":
        return True, document
    if not field_path.startswith("$."):
        return False, None
    current = document
    for segment in field_path[2:].split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _expected_field(field_path: str) -> bool:
    terminal = field_path.rsplit(".", 1)[-1].casefold()
    return terminal in {
        "correct_value",
        "desired",
        "desired_behavior",
        "expected",
        "expected_behavior",
        "expected_output",
        "intended_behavior",
        "required_behavior",
    } or bool(re.search(r"(?:^|_)(?:expected|desired|correct|intended|required)(?:_|$)", terminal))


def _predicate_matches(predicate: Mapping[str, Any], expected: Any) -> bool:
    kind = predicate.get("kind")
    if kind == "equals":
        return predicate.get("expected") == expected
    if kind == "membership":
        return isinstance(expected, list) and predicate.get("members") == expected
    if kind == "range":
        projection = {
            key: predicate.get(key)
            for key in (
                "minimum",
                "maximum",
                "minimum_inclusive",
                "maximum_inclusive",
            )
            if key in predicate
        }
        return isinstance(expected, Mapping) and projection == dict(expected)
    if kind == "schema":
        return predicate.get("schema") == expected
    if kind == "existence":
        return predicate.get("expected") == expected
    if kind == "state_transition":
        return (
            isinstance(expected, Mapping)
            and expected.get("before") == predicate.get("from")
            and expected.get("after") == predicate.get("to")
        ) or predicate.get("to") == expected
    if kind == "event_sequence":
        return predicate.get("events") == expected
    return False


class OriginExactValueBasisAdapter:
    basis_kind = "origin_exact_value"
    adapter_version = "1"

    def bind(self, context: PositiveBasisContext) -> PositiveBasisResult:
        atom_id = context.semantic_claim.get("atom_id")
        field_path = context.semantic_claim.get("field_path")
        if (
            not isinstance(atom_id, str)
            or atom_id not in context.source_atom_ids
            or not isinstance(field_path, str)
            or not _expected_field(field_path)
        ):
            return PositiveBasisResult(diagnostics=("origin_positive_basis_unbound",))
        atom_receipt = next(
            (
                item
                for item in context.evidence_assignment.get("atom_receipts", [])
                if isinstance(item, Mapping) and item.get("atom_id") == atom_id
            ),
            None,
        )
        found, expected = (
            _field_value(atom_receipt.get("atom_snapshot", {}), field_path)
            if isinstance(atom_receipt, Mapping)
            else (False, None)
        )
        if not found or not _predicate_matches(context.predicate, expected):
            return PositiveBasisResult(diagnostics=("origin_positive_basis_mismatch",))
        basis = content_bound_payload(
            {
                "basis_kind": self.basis_kind,
                "basis_adapter_version": self.adapter_version,
                "origin_atom_ids": [atom_id],
                "origin_atom_sha256": atom_receipt.get("atom_sha256"),
                "field_path": field_path,
                "expected_value_sha256": canonical_json_sha256(expected),
                "predicate_sha256": canonical_json_sha256(context.predicate),
                "runner_attested": True,
            },
            hash_field="basis_sha256",
        )
        return PositiveBasisResult(basis=basis)


class RepositoryFailFirstCommandBasisAdapter:
    """Bind the conventional pass contract of a pre-existing repository command."""

    basis_kind = "repository_fail_first_command"
    adapter_version = "1"

    def bind(self, context: PositiveBasisContext) -> PositiveBasisResult:
        baseline_id = context.semantic_claim.get("baseline_experiment_id")
        challenge_id = context.semantic_claim.get("challenge_experiment_id")
        replays = context.clean_replays or {}
        baseline = replays.get(str(baseline_id))
        challenge = replays.get(str(challenge_id))
        baseline_auth = (
            baseline.get("command_authorization") if isinstance(baseline, Mapping) else None
        )
        challenge_auth = (
            challenge.get("command_authorization") if isinstance(challenge, Mapping) else None
        )
        baseline_argv = baseline.get("executed_argv") if isinstance(baseline, Mapping) else None
        challenge_argv = (
            challenge.get("executed_argv") if isinstance(challenge, Mapping) else None
        )
        baseline_identity = command_authorization_identity(baseline_auth)
        challenge_identity = command_authorization_identity(challenge_auth)
        if (
            not isinstance(baseline, Mapping)
            or not isinstance(challenge, Mapping)
            or not isinstance(baseline_auth, Mapping)
            or not isinstance(challenge_auth, Mapping)
            or not isinstance(baseline_argv, list)
            or not isinstance(challenge_argv, list)
            or command_authorization_errors(baseline_auth, argv=baseline_argv)
            or command_authorization_errors(challenge_auth, argv=challenge_argv)
            or baseline_identity is None
            or baseline_identity != challenge_identity
            or not isinstance(baseline.get("exit_code"), int)
            or isinstance(baseline.get("exit_code"), bool)
            or baseline.get("exit_code") == 0
            or challenge.get("exit_code") != 0
            or context.predicate != {"kind": "equals", "expected": 0}
        ):
            return PositiveBasisResult(
                diagnostics=("repository_fail_first_positive_basis_unattested",)
            )
        basis = content_bound_payload(
            {
                "basis_kind": self.basis_kind,
                "basis_adapter_version": self.adapter_version,
                "origin_atom_ids": sorted(context.source_atom_ids),
                "execution_identity": baseline_identity,
                "execution_identity_sha256": canonical_json_sha256(baseline_identity),
                "entrypoint_path": baseline_auth.get("entrypoint_path"),
                "entrypoint_sha256": baseline_auth.get("entrypoint_sha256"),
                "baseline_experiment_id": baseline_id,
                "challenge_experiment_id": challenge_id,
                "baseline_observation_sha256": canonical_json_sha256(
                    {
                        "argv": baseline.get("executed_argv"),
                        "exit_code": baseline.get("exit_code"),
                    }
                ),
                "challenge_observation_sha256": canonical_json_sha256(
                    {
                        "argv": challenge.get("executed_argv"),
                        "exit_code": challenge.get("exit_code"),
                    }
                ),
                "predicate_sha256": canonical_json_sha256(context.predicate),
                "semantic_review_required": False,
                "runner_attested": True,
            },
            hash_field="basis_sha256",
        )
        return PositiveBasisResult(basis=basis)


class AuthenticatedSemanticCitationBasisAdapter:
    """Attest a citation's authenticity while leaving its interpretation for Stage 5."""

    basis_kind = "authenticated_semantic_citation"
    adapter_version = "1"

    def bind(self, context: PositiveBasisContext) -> PositiveBasisResult:
        atom_id = context.semantic_claim.get("atom_id")
        field_path = context.semantic_claim.get("field_path")
        rationale = context.semantic_claim.get("semantic_rationale")
        relation = context.semantic_claim.get("semantic_relation")
        if (
            not isinstance(atom_id, str)
            or atom_id not in context.source_atom_ids
            or not isinstance(field_path, str)
            or not field_path.strip()
            or not isinstance(rationale, str)
            or len(rationale.strip()) < 20
            or not isinstance(relation, str)
            or not relation.strip()
        ):
            return PositiveBasisResult(
                diagnostics=("authenticated_semantic_citation_invalid",)
            )
        atom_receipt = next(
            (
                item
                for item in context.evidence_assignment.get("atom_receipts", [])
                if isinstance(item, Mapping) and item.get("atom_id") == atom_id
            ),
            None,
        )
        found, cited_value = (
            _field_value(atom_receipt.get("atom_snapshot", {}), field_path)
            if isinstance(atom_receipt, Mapping)
            else (False, None)
        )
        if not found:
            return PositiveBasisResult(
                diagnostics=("authenticated_semantic_citation_unresolved",)
            )
        basis = content_bound_payload(
            {
                "basis_kind": self.basis_kind,
                "basis_adapter_version": self.adapter_version,
                "origin_atom_ids": [atom_id],
                "origin_atom_sha256": atom_receipt.get("atom_sha256"),
                "field_path": field_path,
                "cited_value_sha256": canonical_json_sha256(cited_value),
                "semantic_relation": relation.strip(),
                "semantic_rationale": rationale.strip(),
                "predicate_sha256": canonical_json_sha256(context.predicate),
                "semantic_attestation": "authenticated_citation_only",
                "semantic_review_required": True,
                "runner_attested": True,
            },
            hash_field="basis_sha256",
        )
        return PositiveBasisResult(basis=basis)


def builtin_positive_basis_registry() -> PositiveBasisRegistry:
    return PositiveBasisRegistry(
        [
            OriginExactValueBasisAdapter(),
            RepositoryFailFirstCommandBasisAdapter(),
            AuthenticatedSemanticCitationBasisAdapter(),
        ]
    )


__all__ = [
    "OriginExactValueBasisAdapter",
    "RepositoryFailFirstCommandBasisAdapter",
    "AuthenticatedSemanticCitationBasisAdapter",
    "PositiveBasisAdapter",
    "PositiveBasisContext",
    "PositiveBasisRegistry",
    "PositiveBasisResult",
    "builtin_positive_basis_registry",
]
