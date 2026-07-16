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
    builtin_proof_adapter_registry,
)
from backlog_miner.proof_adapters.base import (
    ProofAdapterContext,
    build_receipt,
    observed_value,
)


def test_builtin_registry_only_advertises_live_wired_adapters() -> None:
    adapter_ids = set(builtin_proof_adapter_registry().adapter_ids())

    assert "structured_replay.v1" in adapter_ids
    assert "python_call_chain.v1" not in adapter_ids
    assert "pytest_controlled_difference.v1" not in adapter_ids


def test_event_json_selects_content_bound_json_amid_progress_lines(tmp_path: Path) -> None:
    stdout = tmp_path / "stdout.txt"
    stdout.write_text(
        '{"remaining_image_ids": 3}\n.\n1 passed in 0.02s\n',
        encoding="utf-8",
    )

    ok, value, value_sha256, paths = observed_value(
        {"stdout_path": str(stdout)},
        {"source": "event_json", "json_pointer": "/remaining_image_ids"},
    )

    assert ok is True
    assert value == 3
    assert value_sha256 == canonical_json_sha256(3)
    assert paths == [stdout]


def test_well_formed_but_false_positive_predicate_has_explicit_diagnostic() -> None:
    def replay_inputs(experiment_id: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "source_experiment_id": experiment_id,
            "runner_approved": True,
            "environment": {},
            "disposable_state_paths": [],
        }
        payload["replay_inputs_sha256"] = canonical_json_sha256(payload)
        return payload

    context = ProofAdapterContext(
        case_id="case:test",
        problem_id="problem:test",
        hypothesis_id="hypothesis:test",
        claim={
            "intervention": {
                "kind": "config",
                "target": "config:/agents/codex/config_overrides",
                "predicted_polarity": "removes_failure",
                "before": "warning is fatal",
                "after": "warning is a runtime notice",
            },
            "positive_outcome": {
                "predicate": {"kind": "equals", "expected": True},
            },
        },
        experiments={},
        clean_replays={
            "experiment:baseline": {
                "replay_inputs": replay_inputs("experiment:baseline"),
            },
            "experiment:challenge": {
                "replay_inputs": replay_inputs("experiment:challenge"),
            },
        },
        source_root={},
        planning_workspace=None,
        atom_bindings=[],
        symbol_receipts=[],
        artifact_receipts=[],
        services={},
    )

    result = build_receipt(
        adapter_id="structured_replay.v1",
        adapter_version="1",
        context=context,
        baseline_id="experiment:baseline",
        challenge_id="experiment:challenge",
        baseline_observed=True,
        baseline_observed_sha256=canonical_json_sha256(True),
        baseline_selector={"source": "exit_code"},
        challenge_observed=False,
        challenge_observed_sha256=canonical_json_sha256(False),
        challenge_selector={"source": "exit_code"},
        observation_source="exit_code",
        nodes=[],
        edges=[],
        artifacts=[],
        adapter_evidence={},
    )

    assert result.receipts == ()
    assert result.diagnostics == ("proof_adapter_positive_predicate_not_satisfied:equals",)


def test_causal_contrast_role_is_opt_in_and_legacy_receipt_shape_is_unchanged() -> None:
    def replay_inputs(experiment_id: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "source_experiment_id": experiment_id,
            "runner_approved": True,
            "environment": {},
            "disposable_state_paths": [],
        }
        payload["replay_inputs_sha256"] = canonical_json_sha256(payload)
        return payload

    def build(*, contract_role: str | None) -> dict[str, object]:
        positive: dict[str, object] = {
            "predicate": {"kind": "equals", "expected": 3},
        }
        if contract_role is not None:
            positive["contract_role"] = contract_role
        context = ProofAdapterContext(
            case_id="case:test",
            problem_id="problem:test",
            hypothesis_id="hypothesis:test",
            claim={
                "intervention": {
                    "kind": "age_delta",
                    "target": "cleanup.run",
                    "predicted_polarity": "reduces retained identities",
                },
                "positive_outcome": positive,
            },
            experiments={},
            clean_replays={
                "experiment:fresh": {
                    "replay_inputs": replay_inputs("experiment:fresh"),
                },
                "experiment:aged": {
                    "replay_inputs": replay_inputs("experiment:aged"),
                },
            },
            source_root={
                "origin_atom_ids": ["atom:burst"],
                "positive_basis": {
                    "basis_kind": "authenticated_semantic_citation",
                    "basis_sha256": "a" * 64,
                },
            },
            planning_workspace=None,
            atom_bindings=[],
            symbol_receipts=[],
            artifact_receipts=[],
            services={},
        )
        result = build_receipt(
            adapter_id="structured_replay.v1",
            adapter_version="1",
            context=context,
            baseline_id="experiment:fresh",
            challenge_id="experiment:aged",
            baseline_observed=49,
            baseline_observed_sha256=canonical_json_sha256(49),
            baseline_selector={"source": "event_json"},
            challenge_observed=3,
            challenge_observed_sha256=canonical_json_sha256(3),
            challenge_selector={"source": "event_json"},
            observation_source="event_json",
            nodes=[
                {"node_id": "proof:source"},
                {"node_id": "proof:outcome"},
            ],
            edges=[],
            artifacts=[],
            adapter_evidence={},
        )
        assert result.diagnostics == ()
        return result.receipts[0]

    legacy = build(contract_role=None)
    contrast = build(contract_role="causal_contrast")

    assert "contract_role" not in legacy["positive_outcome"]
    assert contrast["positive_outcome"]["contract_role"] == "causal_contrast"
    assert legacy["proof_receipt_id"] != contrast["proof_receipt_id"]


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
