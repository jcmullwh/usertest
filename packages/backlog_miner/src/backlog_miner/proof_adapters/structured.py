"""General proof adapters backed by retained replay output and inspected state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from backlog_core.causal_proof import canonical_json_sha256, proof_receipt_id_for

from backlog_miner.proof_adapters.base import (
    ProofAdapterContext,
    ProofAdapterResult,
    artifact_receipts,
    build_receipt,
    json_pointer_value,
    observed_value,
    replay_environment_attestation,
    runner_edge,
    runner_node,
    text,
)


def _pair(
    context: ProofAdapterContext,
) -> tuple[str, str, Mapping[str, Any], Mapping[str, Any]] | None:
    baseline_id = text(context.claim.get("baseline_experiment_id"))
    challenge_id = text(context.claim.get("challenge_experiment_id"))
    baseline = context.clean_replays.get(baseline_id or "")
    challenge = context.clean_replays.get(challenge_id or "")
    if (
        baseline_id is None
        or challenge_id is None
        or baseline_id == challenge_id
        or not isinstance(baseline, Mapping)
        or not isinstance(challenge, Mapping)
    ):
        return None
    return baseline_id, challenge_id, baseline, challenge


def _observation_specs(context: ProofAdapterContext) -> tuple[Any, Any] | None:
    raw = context.claim.get("observations")
    if not isinstance(raw, Mapping):
        return None
    return raw.get("baseline"), raw.get("challenge")


def _source_node(context: ProofAdapterContext) -> dict[str, Any]:
    root_kind = context.source_root.get("root_kind")
    return runner_node(
        node_id="proof:source-root",
        kind="source_command" if root_kind == "immutable_source_command" else "source_symptom",
        locator=str(root_kind),
        evidence=context.source_root,
    )


def _observation_source_text(spec: Mapping[str, Any]) -> str:
    """Render a selector without making mapping insertion order part of the proof."""

    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class StructuredReplayAdapter:
    adapter_id = "structured_replay.v1"
    adapter_version = "1"
    mechanism_kind = "command"

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult:
        pair = _pair(context)
        specs = _observation_specs(context)
        if pair is None or specs is None:
            return ProofAdapterResult(diagnostics=("proof_adapter_pair_invalid",))
        baseline_id, challenge_id, baseline_replay, challenge_replay = pair
        baseline_ok, baseline_value, baseline_hash, baseline_paths = observed_value(
            baseline_replay, specs[0]
        )
        challenge_ok, challenge_value, challenge_hash, challenge_paths = observed_value(
            challenge_replay, specs[1]
        )
        if not baseline_ok or not challenge_ok or baseline_hash is None or challenge_hash is None:
            return ProofAdapterResult(diagnostics=("proof_adapter_observation_unavailable",))
        target = text(
            context.claim.get("intervention", {}).get("target")
            if isinstance(context.claim.get("intervention"), Mapping)
            else None
        )
        if target is None:
            return ProofAdapterResult(diagnostics=("proof_adapter_intervention_target_missing",))
        source = _source_node(context)
        mechanism = runner_node(
            node_id="proof:mechanism",
            kind=self.mechanism_kind,
            locator=target,
            evidence={
                "baseline_argv": baseline_replay.get("executed_argv"),
                "challenge_argv": challenge_replay.get("executed_argv"),
            },
        )
        outcome = runner_node(
            node_id="proof:outcome",
            kind="outcome",
            locator=_observation_source_text(specs[1]),
            evidence={"value": challenge_value, "sha256": challenge_hash},
        )
        nodes = [source, mechanism, outcome]
        edges = [
            runner_edge(
                source=source["node_id"],
                target=mechanism["node_id"],
                kind="binds_intervention",
                evidence=baseline_replay.get("command_authorization"),
            ),
            runner_edge(
                source=mechanism["node_id"],
                target=outcome["node_id"],
                kind="changes_observable",
                evidence={"baseline": baseline_hash, "challenge": challenge_hash},
            ),
        ]
        return build_receipt(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            context=context,
            baseline_id=baseline_id,
            challenge_id=challenge_id,
            baseline_observed=baseline_value,
            baseline_observed_sha256=baseline_hash,
            baseline_selector=dict(specs[0]),
            challenge_observed=challenge_value,
            challenge_observed_sha256=challenge_hash,
            challenge_selector=dict(specs[1]),
            observation_source=_observation_source_text(specs[1]),
            nodes=nodes,
            edges=edges,
            artifacts=artifact_receipts(
                [*baseline_paths, *challenge_paths], prefix=self.adapter_id
            ),
            adapter_evidence={
                "baseline_observation_spec": specs[0],
                "challenge_observation_spec": specs[1],
            },
        )


def _entrypoint_path(replay: Mapping[str, Any]) -> Path | None:
    workspace_raw = text(replay.get("workspace_dir"))
    if workspace_raw is None:
        return None
    workspace = Path(workspace_raw).resolve()
    authorization = replay.get("command_authorization")
    recorded = (
        text(authorization.get("entrypoint_path"))
        if isinstance(authorization, Mapping)
        else None
    )
    candidates: list[Path] = []
    if recorded is not None:
        candidates.append((workspace / recorded).resolve())
    argv = replay.get("executed_argv")
    if isinstance(argv, list):
        candidates.extend(
            (workspace / token).resolve()
            for token in argv[1:]
            if isinstance(token, str)
            and not Path(token).is_absolute()
            and ".." not in Path(token).parts
        )
    return next(
        (
            path
            for path in candidates
            if path.is_file() and (path == workspace or workspace in path.parents)
        ),
        None,
    )


def _setup_environment_variable(
    replay: Mapping[str, Any],
    variable: str,
) -> Mapping[str, Any] | None:
    setup = replay.get("replay_setup_receipt")
    if not isinstance(setup, Mapping) or setup.get("runner_applied") is not True:
        return None
    supplied_hash = setup.get("replay_setup_sha256")
    projection = {key: value for key, value in setup.items() if key != "replay_setup_sha256"}
    if supplied_hash != canonical_json_sha256(projection):
        return None
    environment = setup.get("environment")
    value = environment.get(variable) if isinstance(environment, Mapping) else None
    return value if isinstance(value, Mapping) else None


class EnvironmentProofAdapter(StructuredReplayAdapter):
    adapter_id = "environment.v1"
    mechanism_kind = "environment"

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult:
        pair = _pair(context)
        intervention = context.claim.get("intervention")
        target = (
            text(intervention.get("target")) if isinstance(intervention, Mapping) else None
        )
        if pair is None or target is None or not target.startswith("env:"):
            return ProofAdapterResult(diagnostics=("environment_adapter_target_invalid",))
        variable = target.removeprefix("env:")
        if not variable:
            return ProofAdapterResult(diagnostics=("environment_adapter_target_invalid",))
        baseline_attestation = replay_environment_attestation(pair[2])
        challenge_attestation = replay_environment_attestation(pair[3])
        if baseline_attestation is None or challenge_attestation is None:
            return ProofAdapterResult(
                diagnostics=("environment_adapter_runner_attestation_unavailable",)
            )
        baseline_variables = baseline_attestation.get("variables")
        challenge_variables = challenge_attestation.get("variables")
        baseline_variable_raw = (
            baseline_variables.get(variable)
            if isinstance(baseline_variables, Mapping)
            else None
        )
        challenge_variable_raw = (
            challenge_variables.get(variable)
            if isinstance(challenge_variables, Mapping)
            else None
        )
        baseline_variable = (
            dict(baseline_variable_raw)
            if isinstance(baseline_variable_raw, Mapping)
            else {"present": False}
        )
        challenge_variable = (
            dict(challenge_variable_raw)
            if isinstance(challenge_variable_raw, Mapping)
            else {"present": False}
        )
        baseline_applied = _setup_environment_variable(pair[2], variable)
        challenge_applied = _setup_environment_variable(pair[3], variable)
        if (
            baseline_applied != baseline_variable
            or challenge_applied != challenge_variable
            or baseline_variable == challenge_variable
        ):
            return ProofAdapterResult(
                diagnostics=("environment_adapter_target_delta_unattested",)
            )
        source_paths = [_entrypoint_path(pair[2]), _entrypoint_path(pair[3])]
        result = super().build(context)
        if not result.receipts:
            return result
        receipt = dict(result.receipts[0])
        receipt["adapter_evidence"] = {
            **receipt.get("adapter_evidence", {}),
            "environment_variable": variable,
            "baseline_variable_receipt": dict(baseline_variable),
            "challenge_variable_receipt": dict(challenge_variable),
            "baseline_environment_attestation_sha256": baseline_attestation.get(
                "environment_attestation_sha256"
            ),
            "challenge_environment_attestation_sha256": challenge_attestation.get(
                "environment_attestation_sha256"
            ),
            "entrypoint_artifacts": [
                {
                    "path": str(path),
                    "sha256": sha256(path.read_bytes()).hexdigest(),
                }
                for path in source_paths
                if path is not None
            ],
        }
        receipt["artifacts"] = [
            *receipt["artifacts"],
            *artifact_receipts(
                [path for path in source_paths if path is not None], prefix=self.adapter_id
            ),
        ]
        graph = receipt.get("mechanism_graph")
        if isinstance(graph, dict):
            nodes = graph.get("nodes")
            if isinstance(nodes, list):
                for node in nodes:
                    if isinstance(node, dict) and node.get("node_id") == "proof:mechanism":
                        node["evidence_sha256"] = canonical_json_sha256(
                            {
                                "variable": variable,
                                "baseline": baseline_variable,
                                "challenge": challenge_variable,
                                "baseline_attestation": baseline_attestation.get(
                                    "environment_attestation_sha256"
                                ),
                                "challenge_attestation": challenge_attestation.get(
                                    "environment_attestation_sha256"
                                ),
                            }
                        )

        receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)
        return ProofAdapterResult(receipts=(receipt,))


def _state_value(
    workspace: Path,
    raw_path: Any,
    pointer: Any,
    *,
    observation_kind: str,
) -> tuple[bool, Any, Path | None]:
    relative = text(raw_path)
    if relative is None or Path(relative).is_absolute() or ".." in Path(relative).parts:
        return False, None, None
    path = (workspace / relative).resolve()
    if workspace not in path.parents or path.is_symlink():
        return False, None, None
    if observation_kind == "existence":
        exists = path.is_file()
        return True, {"exists": exists}, path
    if not path.is_file():
        return False, None, None
    try:
        if path.suffix.casefold() == ".json":
            document = json.loads(path.read_text(encoding="utf-8"))
            found, value = json_pointer_value(document, str(pointer or ""))
            return found, value, path
        value = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, None, None
    return True, value, path


def _disposable_path_attested(replay: Mapping[str, Any], raw_path: Any) -> bool:
    relative = text(raw_path)
    setup = replay.get("replay_setup_receipt")
    if (
        relative is None
        or not isinstance(setup, Mapping)
        or setup.get("runner_applied") is not True
    ):
        return False
    supplied_hash = setup.get("replay_setup_sha256")
    projection = {key: value for key, value in setup.items() if key != "replay_setup_sha256"}
    if supplied_hash != canonical_json_sha256(projection):
        return False
    requested = PurePosixPath(relative.replace("\\", "/"))
    declared = setup.get("disposable_state_paths")
    return isinstance(declared, list) and any(
        isinstance(root, str)
        and (
            requested == PurePosixPath(root)
            or PurePosixPath(root) in requested.parents
        )
        for root in declared
    )


class FilesystemStateProofAdapter(StructuredReplayAdapter):
    adapter_id = "filesystem_state.v1"
    mechanism_kind = "filesystem"

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult:
        pair = _pair(context)
        state_inputs = context.claim.get("state_inputs")
        if pair is None or not isinstance(state_inputs, Mapping):
            return ProofAdapterResult(diagnostics=("filesystem_adapter_state_inputs_invalid",))
        baseline_workspace_raw = text(pair[2].get("workspace_dir"))
        challenge_workspace_raw = text(pair[3].get("workspace_dir"))
        if baseline_workspace_raw is None or challenge_workspace_raw is None:
            return ProofAdapterResult(diagnostics=("filesystem_adapter_workspace_unavailable",))
        baseline_workspace = Path(baseline_workspace_raw).resolve()
        challenge_workspace = Path(challenge_workspace_raw).resolve()
        if not baseline_workspace.is_dir() or not challenge_workspace.is_dir():
            return ProofAdapterResult(diagnostics=("filesystem_adapter_workspace_unavailable",))
        observation_kind = text(state_inputs.get("observation_kind")) or "value"
        if observation_kind not in {"value", "existence"}:
            return ProofAdapterResult(diagnostics=("filesystem_adapter_observation_invalid",))
        if not _disposable_path_attested(
            pair[2], state_inputs.get("baseline_path")
        ) or not _disposable_path_attested(pair[3], state_inputs.get("challenge_path")):
            return ProofAdapterResult(diagnostics=("filesystem_adapter_state_unattested",))
        baseline_ok, baseline_value, baseline_path = _state_value(
            baseline_workspace,
            state_inputs.get("baseline_path"),
            state_inputs.get("json_pointer"),
            observation_kind=observation_kind,
        )
        challenge_ok, challenge_value, challenge_path = _state_value(
            challenge_workspace,
            state_inputs.get("challenge_path"),
            state_inputs.get("json_pointer"),
            observation_kind=observation_kind,
        )
        if not baseline_ok or not challenge_ok:
            return ProofAdapterResult(diagnostics=("filesystem_adapter_state_unavailable",))
        specs = {
            "baseline": {
                "source": "workspace_state",
                "path": str(state_inputs.get("baseline_path") or "").replace("\\", "/"),
                "observation_kind": observation_kind,
                "json_pointer": state_inputs.get("json_pointer"),
            },
            "challenge": {
                "source": "workspace_state",
                "path": str(state_inputs.get("challenge_path") or "").replace("\\", "/"),
                "observation_kind": observation_kind,
                "json_pointer": state_inputs.get("json_pointer"),
            },
        }
        source = _source_node(context)
        state = runner_node(
            node_id="proof:state",
            kind=self.mechanism_kind,
            locator=str(context.claim.get("intervention", {}).get("target")),
            evidence={
                "baseline_path": str(baseline_path) if baseline_path is not None else None,
                "baseline_sha256": (
                    sha256(baseline_path.read_bytes()).hexdigest()
                    if baseline_path is not None and baseline_path.is_file()
                    else None
                ),
                "challenge_path": str(challenge_path) if challenge_path is not None else None,
                "challenge_sha256": (
                    sha256(challenge_path.read_bytes()).hexdigest()
                    if challenge_path is not None and challenge_path.is_file()
                    else None
                ),
                "observation_kind": observation_kind,
                "baseline_parent_listing_sha256": (
                    canonical_json_sha256(
                        sorted(item.name for item in baseline_path.parent.iterdir())
                    )
                    if baseline_path is not None and baseline_path.parent.is_dir()
                    else None
                ),
                "challenge_parent_listing_sha256": (
                    canonical_json_sha256(
                        sorted(item.name for item in challenge_path.parent.iterdir())
                    )
                    if challenge_path is not None and challenge_path.parent.is_dir()
                    else None
                ),
            },
        )
        outcome = runner_node(
            node_id="proof:outcome",
            kind="outcome",
            locator=str(state_inputs.get("json_pointer") or "$"),
            evidence=challenge_value,
        )
        positive_raw = context.claim.get("positive_outcome")
        predicate = positive_raw.get("predicate") if isinstance(positive_raw, Mapping) else {}
        positive_observed = (
            {"before": baseline_value, "after": challenge_value}
            if isinstance(predicate, Mapping) and predicate.get("kind") == "state_transition"
            else challenge_value
        )
        return build_receipt(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            context=context,
            baseline_id=pair[0],
            challenge_id=pair[1],
            baseline_observed=baseline_value,
            baseline_observed_sha256=canonical_json_sha256(baseline_value),
            baseline_selector=specs["baseline"],
            challenge_observed=challenge_value,
            challenge_observed_sha256=canonical_json_sha256(challenge_value),
            challenge_selector=specs["challenge"],
            observation_source="filesystem_state",
            nodes=[source, state, outcome],
            edges=[
                runner_edge(
                    source=source["node_id"],
                    target=state["node_id"],
                    kind="selects_state_input",
                    evidence=state_inputs,
                ),
                runner_edge(
                    source=state["node_id"],
                    target=outcome["node_id"],
                    kind="changes_state",
                    evidence={"before": baseline_value, "after": challenge_value},
                ),
            ],
            artifacts=artifact_receipts(
                [
                    path
                    for path in (baseline_path, challenge_path)
                    if path is not None and path.is_file()
                ],
                prefix=self.adapter_id,
            ),
            adapter_evidence={"state_inputs": specs},
            positive_observed=positive_observed,
        )


class ConfigRepositoryStateProofAdapter(FilesystemStateProofAdapter):
    adapter_id = "config_repository_state.v1"
    mechanism_kind = "configuration"


class PlatformProofAdapter(StructuredReplayAdapter):
    adapter_id = "platform.v1"
    mechanism_kind = "platform"

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult:
        pair = _pair(context)
        if pair is None:
            return ProofAdapterResult(diagnostics=("platform_adapter_pair_invalid",))
        baseline_isolation = pair[2].get("execution_isolation")
        challenge_isolation = pair[3].get("execution_isolation")
        if not isinstance(baseline_isolation, Mapping) or not isinstance(
            challenge_isolation, Mapping
        ):
            return ProofAdapterResult(diagnostics=("platform_adapter_isolation_unavailable",))
        baseline_experiment = context.experiments.get(pair[0], {})
        challenge_experiment = context.experiments.get(pair[1], {})
        baseline_value = baseline_isolation.get("platform")
        challenge_value = challenge_isolation.get("platform")
        actual = challenge_isolation.get("platform")
        if not isinstance(baseline_value, str) or not isinstance(challenge_value, str):
            return ProofAdapterResult(diagnostics=("platform_adapter_platform_unavailable",))
        source = _source_node(context)
        platform_node = runner_node(
            node_id="proof:platform",
            kind="platform",
            locator=str(actual),
            evidence={"baseline": baseline_isolation, "challenge": challenge_isolation},
        )
        outcome = runner_node(
            node_id="proof:outcome",
            kind="outcome",
            locator="execution_isolation.platform",
            evidence=actual,
        )
        paths = [
            Path(path)
            for replay in (pair[2], pair[3])
            for stream in ("stdout", "stderr")
            for path in [text(replay.get(f"{stream}_path"))]
            if path is not None
        ]
        return build_receipt(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            context=context,
            baseline_id=pair[0],
            challenge_id=pair[1],
            baseline_observed=baseline_value,
            baseline_observed_sha256=canonical_json_sha256(baseline_value),
            baseline_selector={"source": "platform"},
            challenge_observed=challenge_value,
            challenge_observed_sha256=canonical_json_sha256(challenge_value),
            challenge_selector={"source": "platform"},
            observation_source="execution_isolation.platform",
            nodes=[source, platform_node, outcome],
            edges=[
                runner_edge(
                    source=source["node_id"],
                    target=platform_node["node_id"],
                    kind="requires_platform",
                    evidence=challenge_experiment.get("platform_requirement"),
                ),
                runner_edge(
                    source=platform_node["node_id"],
                    target=outcome["node_id"],
                    kind="routes_execution",
                    evidence=challenge_isolation,
                ),
            ],
            artifacts=artifact_receipts(paths, prefix=self.adapter_id),
            adapter_evidence={
                "baseline_isolation": baseline_isolation,
                "challenge_isolation": challenge_isolation,
                "baseline_requirement": baseline_experiment.get(
                    "platform_requirement", "any"
                ),
                "challenge_requirement": challenge_experiment.get(
                    "platform_requirement", "any"
                ),
            },
            positive_observed=actual,
        )


class CommandTraceProofAdapter(StructuredReplayAdapter):
    adapter_id = "command_trace.v1"

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult:
        pair = _pair(context)
        specs = _observation_specs(context)
        if pair is None or specs is None:
            return ProofAdapterResult(diagnostics=("command_trace_pair_invalid",))
        challenge_argv = pair[3].get("executed_argv")
        if not isinstance(challenge_argv, list) or not challenge_argv:
            return ProofAdapterResult(diagnostics=("command_trace_argv_unavailable",))
        result = super().build(context)
        if not result.receipts:
            return result
        receipt = dict(result.receipts[0])
        receipt["adapter_evidence"] = {
            **receipt.get("adapter_evidence", {}),
            "runtime": Path(str(challenge_argv[0])).name,
            "executed_argv_sha256": canonical_json_sha256(challenge_argv),
        }
        receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)
        return ProofAdapterResult(receipts=(receipt,))
