"""Open registry for runner-minted positive-outcome semantic bases."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
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
    inspected_file_receipts: Sequence[Mapping[str, Any]] = ()
    mechanism_symbols: frozenset[str] = frozenset()
    config_value_resolver: Callable[..., tuple[bool, Any, str | None]] | None = None


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
            return PositiveBasisResult(diagnostics=(f"positive_basis_adapter_unavailable:{kind}",))
        return adapter.bind(context)


def _field_value(document: Any, field_path: str) -> tuple[bool, Any]:
    if field_path == "$":
        return True, document
    current = document
    cursor = 1
    while cursor < len(field_path):
        if field_path[cursor] == ".":
            match = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)", field_path[cursor:])
            if match is None or not isinstance(current, Mapping):
                return False, None
            key = match.group(1)
            if key not in current:
                return False, None
            current = current[key]
            cursor += len(match.group(0))
            continue
        if field_path[cursor] == "[":
            match = re.match(r"\[(\d+)\]", field_path[cursor:])
            if match is None or not isinstance(current, list):
                return False, None
            index = int(match.group(1))
            if index >= len(current):
                return False, None
            current = current[index]
            cursor += len(match.group(0))
            continue
        # Keep the citation grammar intentionally restricted to JSON-like object keys and
        # non-negative list indexes. Wildcards, slices, negative indexes, and quoted-key
        # expressions would make the content binding ambiguous.
        if cursor == 1 and not field_path.startswith("$."):
            return False, None
        return False, None
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


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def repository_contract_quote_provenance(
    semantic_claim: Mapping[str, Any],
    *,
    planning_workspace: Any,
    inspected_file_receipts: Sequence[Mapping[str, Any]],
    inspected_symbol_receipts: Sequence[Mapping[str, Any]],
    mechanism_symbols: frozenset[str] | set[str],
    config_value_resolver: Callable[..., tuple[bool, Any, str | None]] | None = None,
) -> dict[str, Any] | None:
    """Authenticate a repository quote with the legacy outcome-oracle contract.

    The proof-adapter registry and retained-harness outcome oracle deliberately use this
    one binder. A quote is never accepted from model text alone: its file must be an
    attested read at the planning revision, and API quotes must additionally be inside
    the unique attested mechanism symbol that the claim names.
    """

    workspace = Path(planning_workspace).resolve() if planning_workspace is not None else None
    path_raw = _text(semantic_claim.get("path"))
    exact_quote = _text(semantic_claim.get("exact_quote"))
    contract_type = semantic_claim.get("contract_type")
    allowed_suffixes = {
        "api_contract": {".py", ".pyi"},
        "documentation": {".md", ".rst", ".txt"},
        "schema": {".json", ".toml", ".yaml", ".yml"},
    }
    relative = Path(path_raw or "")
    path = (workspace / relative).resolve() if workspace is not None else relative
    normalized_relative = relative.as_posix()
    inspected = next(
        (
            dict(receipt)
            for receipt in inspected_file_receipts
            if receipt.get("path") == normalized_relative
        ),
        None,
    )
    if (
        workspace is None
        or path_raw is None
        or exact_quote is None
        or contract_type not in allowed_suffixes
        or relative.is_absolute()
        or ".." in relative.parts
        or path.suffix.casefold() not in allowed_suffixes[str(contract_type)]
        or not _within(path, workspace)
        or not path.is_file()
        or path.is_symlink()
        or not isinstance(inspected, dict)
    ):
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    if exact_quote not in content or inspected.get("sha256") != _sha256_path(path):
        return None

    locator: dict[str, Any]
    if contract_type == "api_contract":
        symbol = _text(semantic_claim.get("symbol"))
        symbol_receipt = next(
            (
                receipt
                for receipt in inspected_symbol_receipts
                if receipt.get("symbol") == symbol and receipt.get("path") == normalized_relative
            ),
            None,
        )
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return None
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and symbol is not None
            and symbol.replace(":", ".").replace("#", ".").endswith(node.name)
        ]
        segment = ast.get_source_segment(content, candidates[0]) if len(candidates) == 1 else None
        if (
            symbol not in mechanism_symbols
            or not isinstance(symbol_receipt, Mapping)
            or not isinstance(segment, str)
            or exact_quote not in segment
        ):
            return None
        locator = {"kind": "python_symbol", "symbol": symbol}
    elif contract_type == "schema":
        pointer = _text(semantic_claim.get("json_pointer"))
        if config_value_resolver is None or pointer is None or not pointer.startswith("/"):
            return None
        exists, schema_value, _format_name = config_value_resolver(
            path=path,
            symbol=f"config:{pointer}",
        )
        if not exists:
            return None
        locator = {
            "kind": "schema_pointer",
            "json_pointer": pointer,
            "value_sha256": canonical_json_sha256(schema_value),
        }
    else:
        subject = _text(semantic_claim.get("contract_subject"))
        allowed_subjects = set(mechanism_symbols) | {
            symbol.rsplit(".", 1)[-1] for symbol in mechanism_symbols
        }
        if subject not in allowed_subjects or subject not in exact_quote:
            return None
        locator = {"kind": "mechanism_subject", "subject": subject}

    return {
        "kind": "repository_contract_quote",
        "verification_method": "runner_researched_repository_contract_quote_v1",
        "contract_type": contract_type,
        "path": normalized_relative,
        "sha256": inspected.get("sha256"),
        "git_blob_sha": inspected.get("git_blob_sha"),
        "read_event_sha256": inspected.get("read_event_sha256"),
        "exact_quote": exact_quote,
        "exact_quote_sha256": sha256(exact_quote.encode("utf-8")).hexdigest(),
        "contract_locator": locator,
    }


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
        challenge_argv = challenge.get("executed_argv") if isinstance(challenge, Mapping) else None
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


class RepositoryContractQuoteBasisAdapter:
    """Bind an authenticated repository contract quote to a positive predicate."""

    basis_kind = "repository_contract_quote"
    adapter_version = "1"

    def bind(self, context: PositiveBasisContext) -> PositiveBasisResult:
        provenance = repository_contract_quote_provenance(
            context.semantic_claim,
            planning_workspace=context.planning_workspace,
            inspected_file_receipts=context.inspected_file_receipts,
            inspected_symbol_receipts=context.symbol_receipts,
            mechanism_symbols=context.mechanism_symbols,
            config_value_resolver=context.config_value_resolver,
        )
        if provenance is None:
            return PositiveBasisResult(
                diagnostics=("repository_contract_quote_positive_basis_unattested",)
            )
        basis = content_bound_payload(
            {
                "basis_kind": self.basis_kind,
                "basis_adapter_version": self.adapter_version,
                "origin_atom_ids": sorted(context.source_atom_ids),
                "predicate_sha256": canonical_json_sha256(context.predicate),
                "provenance": provenance,
                "semantic_review_required": True,
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
            return PositiveBasisResult(diagnostics=("authenticated_semantic_citation_invalid",))
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
            return PositiveBasisResult(diagnostics=("authenticated_semantic_citation_unresolved",))
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
            RepositoryContractQuoteBasisAdapter(),
            AuthenticatedSemanticCitationBasisAdapter(),
        ]
    )


__all__ = [
    "OriginExactValueBasisAdapter",
    "RepositoryContractQuoteBasisAdapter",
    "RepositoryFailFirstCommandBasisAdapter",
    "AuthenticatedSemanticCitationBasisAdapter",
    "PositiveBasisAdapter",
    "PositiveBasisContext",
    "PositiveBasisRegistry",
    "PositiveBasisResult",
    "builtin_positive_basis_registry",
    "repository_contract_quote_provenance",
]
