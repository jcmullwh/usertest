from __future__ import annotations

from backlog_core.causal_proof import canonical_json_sha256, content_bound_payload

from backlog_miner.proof_adapters import (
    AuthenticatedSemanticCitationBasisAdapter,
    OriginExactValueBasisAdapter,
    PositiveBasisContext,
    RepositoryFailFirstCommandBasisAdapter,
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
    assert result.basis["execution_identity"]["identity_kind"] == (
        "repository_bindings"
    )
    assert result.basis["execution_identity"]["repository_binding_sha256s"] == [
        binding["repository_binding_sha256"]
    ]
