from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from backlog_core.causal_proof import canonical_json_sha256, content_bound_payload

from backlog_miner.proof_adapters import (
    AuthenticatedSemanticCitationBasisAdapter,
    OriginExactValueBasisAdapter,
    PositiveBasisContext,
    RepositoryContractQuoteBasisAdapter,
    RepositoryFailFirstCommandBasisAdapter,
    builtin_positive_basis_registry,
)


def _basis_context(
    *,
    kind: str,
    field_path: str,
) -> PositiveBasisContext:
    atom = {
        "acceptance_text": "A completed run returns the durable ready record.",
        "expected_status": "ready",
    }
    return PositiveBasisContext(
        semantic_claim={
            "kind": kind,
            "atom_id": "atom:contract",
            "field_path": field_path,
            "semantic_relation": "documented domain acceptance contract",
            "semantic_rationale": (
                "This authenticated sentence describes the intended durable outcome, "
                "but Stage 5 must still judge whether the proposed predicate captures it."
            ),
        },
        predicate={"kind": "equals", "expected": "ready"},
        source_atom_ids=frozenset({"atom:contract"}),
        evidence_assignment={
            "atom_receipts": [
                {
                    "atom_id": "atom:contract",
                    "atom_sha256": canonical_json_sha256(atom),
                    "atom_snapshot": atom,
                }
            ]
        },
    )


def test_authenticated_semantic_citation_accepts_non_expected_field_for_review() -> None:
    result = AuthenticatedSemanticCitationBasisAdapter().bind(
        _basis_context(
            kind="authenticated_semantic_citation",
            field_path="$.acceptance_text",
        )
    )

    assert result.diagnostics == ()
    assert result.basis is not None
    assert result.basis["semantic_review_required"] is True
    assert result.basis["semantic_attestation"] == "authenticated_citation_only"


def test_exact_value_basis_remains_mechanical_and_rejects_narrative_field() -> None:
    result = OriginExactValueBasisAdapter().bind(
        _basis_context(
            kind="origin_exact_value",
            field_path="$.acceptance_text",
        )
    )

    assert result.basis is None
    assert result.diagnostics == ("origin_positive_basis_unbound",)


def test_repository_fail_first_basis_accepts_open_tracked_binding_identity() -> None:
    binding = content_bound_payload(
        {
            "path": "Cargo.toml",
            "relationship": "Tracked project manifest governing this runner.",
            "file_sha256": "a" * 64,
            "git_blob_sha": "b" * 40,
            "runner_attested": True,
        },
        hash_field="repository_binding_sha256",
    )

    def authorization(argv: list[str]) -> dict[str, object]:
        return content_bound_payload(
            {
                "authorization_kind": "future_repo_native_runner",
                "executed_argv_sha256": canonical_json_sha256(argv),
                "shell": False,
                "workspace_confined": True,
                "repository_bindings": [binding],
                "runner_attested": True,
            },
            hash_field="authorization_sha256",
        )

    baseline_argv = ["cargo", "test"]
    challenge_argv = ["cargo", "test"]
    result = RepositoryFailFirstCommandBasisAdapter().bind(
        PositiveBasisContext(
            semantic_claim={
                "kind": "repository_fail_first_command",
                "baseline_experiment_id": "experiment:baseline",
                "challenge_experiment_id": "experiment:challenge",
            },
            predicate={"kind": "equals", "expected": 0},
            source_atom_ids=frozenset({"atom:failure"}),
            evidence_assignment={},
            clean_replays={
                "experiment:baseline": {
                    "executed_argv": baseline_argv,
                    "command_authorization": authorization(baseline_argv),
                    "exit_code": 1,
                },
                "experiment:challenge": {
                    "executed_argv": challenge_argv,
                    "command_authorization": authorization(challenge_argv),
                    "exit_code": 0,
                },
            },
        )
    )

    assert result.diagnostics == ()
    assert result.basis is not None
    assert result.basis["execution_identity"]["identity_kind"] == ("repository_bindings")
    assert result.basis["execution_identity"]["repository_binding_sha256s"] == [
        binding["repository_binding_sha256"]
    ]


def test_repository_contract_quote_basis_uses_attested_file_and_symbol(
    tmp_path: Path,
) -> None:
    relative = "src/capability.py"
    source = tmp_path / relative
    source.parent.mkdir()
    quote = "available is the only dispatch-enabling state"
    source.write_text(
        f'def resolve_capability():\n    """{quote}."""\n    return "available"\n',
        encoding="utf-8",
    )

    def context(*, file_sha256: str, symbol_receipts: list[dict[str, object]]):
        return PositiveBasisContext(
            semantic_claim={
                "kind": "repository_contract_quote",
                "contract_type": "api_contract",
                "path": relative,
                "symbol": "resolve_capability",
                "exact_quote": quote,
            },
            predicate={"kind": "equals", "expected": "available"},
            source_atom_ids=frozenset({"atom:shell"}),
            evidence_assignment={},
            planning_workspace=tmp_path,
            inspected_file_receipts=[
                {
                    "path": relative,
                    "sha256": file_sha256,
                    "git_blob_sha": "a" * 40,
                    "read_event_sha256": "b" * 64,
                }
            ],
            symbol_receipts=symbol_receipts,
            mechanism_symbols=frozenset({"resolve_capability"}),
        )

    file_sha256 = sha256(source.read_bytes()).hexdigest()
    symbol_receipts = [{"path": relative, "symbol": "resolve_capability"}]
    result = builtin_positive_basis_registry().evaluate(
        context(file_sha256=file_sha256, symbol_receipts=symbol_receipts)
    )

    assert result.diagnostics == ()
    assert result.basis is not None
    assert result.basis["provenance"]["verification_method"] == (
        "runner_researched_repository_contract_quote_v1"
    )
    assert result.basis["provenance"]["contract_locator"] == {
        "kind": "python_symbol",
        "symbol": "resolve_capability",
    }

    missing_symbol = RepositoryContractQuoteBasisAdapter().bind(
        context(file_sha256=file_sha256, symbol_receipts=[])
    )
    assert missing_symbol.basis is None
    assert missing_symbol.diagnostics == ("repository_contract_quote_positive_basis_unattested",)

    wrong_hash = RepositoryContractQuoteBasisAdapter().bind(
        context(file_sha256="0" * 64, symbol_receipts=symbol_receipts)
    )
    assert wrong_hash.basis is None

    source.write_text(
        f'CONTRACT = "{quote}"\n\ndef resolve_capability():\n    return "available"\n',
        encoding="utf-8",
    )
    quote_outside_symbol = RepositoryContractQuoteBasisAdapter().bind(
        context(
            file_sha256=sha256(source.read_bytes()).hexdigest(),
            symbol_receipts=symbol_receipts,
        )
    )
    assert quote_outside_symbol.basis is None
