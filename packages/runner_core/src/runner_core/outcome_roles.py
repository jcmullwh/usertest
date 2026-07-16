from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from runner_core.verification_commands import verification_command_safety_errors

OUTCOME_EVIDENCE_ROLES = frozenset(
    {"original_scenario", "live", "mitigation_effect", "recurrence"}
)
_CENTRALIZED_RECURRENCE_OWNER = "centralized_case_refresh"
_SENSITIVE_ENVIRONMENT_RE = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE|SESSION|"
    r"^AWS_|^AZURE_|^GOOGLE_|^GCP_|^GH_|^GITHUB_|^SSH_)",
    re.IGNORECASE,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


CausalPredicateEvaluator = Callable[[Mapping[str, Any], Any], tuple[bool, str | None]]
CausalObservationReader = Callable[
    [Mapping[str, Any], list[dict[str, Any]], Path],
    tuple[bool, Any, str | None],
]

_CAUSAL_PREDICATE_EVALUATORS: dict[str, CausalPredicateEvaluator] = {}
_CAUSAL_OBSERVATION_READERS: dict[str, CausalObservationReader] = {}


def register_causal_outcome_predicate(
    kind: str,
    evaluator: CausalPredicateEvaluator,
    *,
    replace: bool = False,
) -> None:
    """Register an adapter-neutral predicate without editing the outcome runner."""

    normalized = kind.strip() if isinstance(kind, str) else ""
    if not normalized or not callable(evaluator):
        raise ValueError("causal_outcome_predicate_registration_invalid")
    if normalized in _CAUSAL_PREDICATE_EVALUATORS and not replace:
        raise ValueError(f"causal_outcome_predicate_already_registered:{normalized}")
    _CAUSAL_PREDICATE_EVALUATORS[normalized] = evaluator


def register_causal_observation_source(
    source: str,
    reader: CausalObservationReader,
    *,
    replace: bool = False,
) -> None:
    """Register a portable post-change observation source."""

    normalized = source.strip() if isinstance(source, str) else ""
    if not normalized or not callable(reader):
        raise ValueError("causal_observation_source_registration_invalid")
    if normalized in _CAUSAL_OBSERVATION_READERS and not replace:
        raise ValueError(f"causal_observation_source_already_registered:{normalized}")
    _CAUSAL_OBSERVATION_READERS[normalized] = reader


def _schema_observation_errors(value: Any, schema: Any, *, path: str = "$") -> list[str]:
    if not isinstance(schema, Mapping):
        return [f"schema_invalid:{path}"]
    errors: list[str] = []
    expected_type = schema.get("type")
    checks: dict[str, Callable[[Any], bool]] = {
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": lambda item: (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
        ),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected_type is not None:
        check = checks.get(str(expected_type))
        if check is None or not check(value):
            errors.append(f"schema_type_mismatch:{path}:{expected_type}")
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            errors.append(f"schema_required_invalid:{path}")
        else:
            errors.extend(
                f"schema_required_missing:{path}.{key}" for key in required if key not in value
            )
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            errors.append(f"schema_properties_invalid:{path}")
        else:
            for key, child in properties.items():
                if isinstance(key, str) and key in value:
                    errors.extend(
                        _schema_observation_errors(value[key], child, path=f"{path}.{key}")
                    )
    if isinstance(value, list) and "items" in schema:
        errors.extend(
            error
            for index, item in enumerate(value)
            for error in _schema_observation_errors(
                item,
                schema["items"],
                path=f"{path}[{index}]",
            )
        )
    return errors


def _evaluate_builtin_causal_predicate(
    predicate: Mapping[str, Any], observed: Any
) -> tuple[bool, str | None]:
    kind = predicate.get("kind")
    if kind == "equals":
        return observed == predicate.get("expected"), None
    if kind == "membership":
        members = predicate.get("members")
        return (
            observed in members if isinstance(members, list) else False,
            None if isinstance(members, list) else "predicate_members_invalid",
        )
    if kind == "range":
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            return False, "predicate_range_observation_invalid"
        minimum = predicate.get("minimum")
        maximum = predicate.get("maximum")
        if minimum is None and maximum is None:
            return False, "predicate_range_unbounded"
        passed = True
        if minimum is not None:
            passed = passed and (
                observed >= minimum
                if predicate.get("minimum_inclusive", True) is True
                else observed > minimum
            )
        if maximum is not None:
            passed = passed and (
                observed <= maximum
                if predicate.get("maximum_inclusive", True) is True
                else observed < maximum
            )
        return passed, None
    if kind == "schema":
        errors = _schema_observation_errors(observed, predicate.get("schema"))
        return not errors, ";".join(errors) if errors else None
    if kind == "existence":
        if not isinstance(observed, Mapping) or not isinstance(observed.get("exists"), bool):
            return False, "predicate_existence_observation_invalid"
        return observed.get("exists") is predicate.get("expected"), None
    if kind == "state_transition":
        if not isinstance(observed, Mapping):
            return False, "predicate_state_transition_observation_invalid"
        return (
            observed.get("before") == predicate.get("from")
            and observed.get("after") == predicate.get("to"),
            None,
        )
    if kind == "event_sequence":
        events = predicate.get("events")
        if not isinstance(observed, list) or not isinstance(events, list) or not events:
            return False, "predicate_event_sequence_observation_invalid"
        if predicate.get("mode", "exact") == "exact":
            return observed == events, None
        iterator = iter(observed)
        return all(any(candidate == event for candidate in iterator) for event in events), None
    return False, f"predicate_kind_unsupported:{kind}"


for _predicate_kind in (
    "equals",
    "membership",
    "range",
    "schema",
    "existence",
    "state_transition",
    "event_sequence",
):
    register_causal_outcome_predicate(
        _predicate_kind,
        _evaluate_builtin_causal_predicate,
    )


def _json_pointer_value(value: Any, pointer: str) -> tuple[bool, Any]:
    if pointer == "":
        return True, value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False, None
    current = value
    for raw_segment in pointer[1:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return False, None
    return True, current


def _command_observation(
    selector: Mapping[str, Any],
    commands: list[dict[str, Any]],
    _workspace: Path,
) -> tuple[bool, Any, str | None]:
    command_index = selector.get("command_index", 0)
    if (
        isinstance(command_index, bool)
        or not isinstance(command_index, int)
        or command_index < 0
        or command_index >= len(commands)
    ):
        return False, None, "causal_observation_command_index_invalid"
    command = commands[command_index]
    source = selector.get("source")
    if source == "exit_code":
        return True, command.get("exit_code"), None
    if source in {"stdout_text", "stderr_text", "combined_text", "event_lines"}:
        stream = (
            f"{command.get('stdout') or ''}{command.get('stderr') or ''}"
            if source == "combined_text"
            else command.get("stdout")
            if source in {"stdout_text", "event_lines"}
            else command.get("stderr")
        )
        if not isinstance(stream, str):
            return False, None, "causal_observation_stream_invalid"
        observed: Any = (
            [line for line in stream.splitlines() if line]
            if source == "event_lines"
            else stream
        )
        return True, observed, None
    if source in {"stdout_json", "stderr_json", "event_json"}:
        stream = command.get("stdout") if source != "stderr_json" else command.get("stderr")
        if not isinstance(stream, str):
            return False, None, "causal_observation_stream_invalid"
        try:
            document = json.loads(stream)
        except json.JSONDecodeError:
            return False, None, "causal_observation_json_invalid"
        found, observed = _json_pointer_value(document, str(selector.get("json_pointer") or ""))
        return (
            (True, observed, None)
            if found
            else (False, None, "causal_observation_json_pointer_missing")
        )
    return False, None, f"causal_observation_source_unsupported:{source}"


def _platform_observation(
    _selector: Mapping[str, Any],
    _commands: list[dict[str, Any]],
    _workspace: Path,
) -> tuple[bool, Any, str | None]:
    return True, platform.system().casefold(), None


def _workspace_state_observation(
    selector: Mapping[str, Any],
    _commands: list[dict[str, Any]],
    workspace: Path,
) -> tuple[bool, Any, str | None]:
    path_raw = selector.get("path")
    if not isinstance(path_raw, str) or not path_raw.strip():
        return False, None, "causal_observation_workspace_path_invalid"
    relative = Path(path_raw)
    path = (workspace / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.is_relative_to(workspace)
        or path.is_symlink()
    ):
        return False, None, "causal_observation_workspace_path_unsafe"
    observation_kind = selector.get("observation_kind", "value")
    if observation_kind == "existence":
        return True, {"exists": path.exists()}, None
    if not path.is_file():
        return False, None, "causal_observation_workspace_file_missing"
    try:
        if selector.get("format") == "json" or path.suffix.casefold() == ".json":
            document = json.loads(path.read_text(encoding="utf-8"))
            found, observed = _json_pointer_value(
                document,
                str(selector.get("json_pointer") or ""),
            )
            return (
                (True, observed, None)
                if found
                else (False, None, "causal_observation_json_pointer_missing")
            )
        return True, path.read_text(encoding="utf-8", errors="strict"), None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, None, "causal_observation_workspace_read_failed"


for _command_source in (
    "exit_code",
    "stdout_text",
    "stderr_text",
    "combined_text",
    "event_lines",
    "stdout_json",
    "stderr_json",
    "event_json",
):
    register_causal_observation_source(_command_source, _command_observation)
register_causal_observation_source("platform", _platform_observation)
register_causal_observation_source("workspace_state", _workspace_state_observation)


def _required_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in value
    ):
        raise ValueError(f"outcome_role_invalid_sha256:{field}")
    return value.casefold()


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"outcome_role_required_text:{field}")
    return value.strip()


def _utc_timestamp(value: Any, *, field: str) -> datetime:
    text = _required_text(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"outcome_role_timestamp_invalid:{field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"outcome_role_timestamp_not_utc:{field}")
    return parsed.astimezone(timezone.utc)


def _load_json_object(path: Path, *, error: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(raw, dict):
        raise ValueError(error)
    return raw


def _source_observation_window(atoms_snapshot: Path) -> dict[str, Any]:
    by_run: dict[str, dict[str, Any]] = {}
    try:
        lines = atoms_snapshot.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("outcome_recurrence_atoms_snapshot_unreadable") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            atom = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"outcome_recurrence_atoms_snapshot_invalid:{line_number}"
            ) from exc
        if not isinstance(atom, dict):
            continue
        atom_id = atom.get("atom_id")
        if not isinstance(atom_id, str) or atom_id.startswith("__aggregate__/"):
            continue
        if atom.get("evidence_role") not in {None, "observation"}:
            continue
        if atom.get("idea_originated") is True or str(atom.get("source") or "").casefold() in {
            "idea",
            "ideas",
            "external_idea",
        }:
            continue
        run_rel = atom.get("run_rel")
        if not isinstance(run_rel, str) or not run_rel.strip():
            continue
        timestamp = atom.get("timestamp_utc")
        timestamp_text = timestamp.strip() if isinstance(timestamp, str) else None
        row = by_run.setdefault(
            run_rel.strip(),
            {
                "run_rel": run_rel.strip(),
                "source_atom_count": 0,
                "latest_timestamp_utc": None,
            },
        )
        row["source_atom_count"] = int(row["source_atom_count"]) + 1
        previous = row.get("latest_timestamp_utc")
        if timestamp_text and (not isinstance(previous, str) or timestamp_text > previous):
            row["latest_timestamp_utc"] = timestamp_text
    runs = [by_run[key] for key in sorted(by_run)]
    summary = {
        "source_run_count": len(runs),
        "source_atom_count": sum(int(row["source_atom_count"]) for row in runs),
        "runs": runs,
    }
    return {**summary, "summary_sha256": _sha256_json(summary)}


def _validate_recurrence_refresh(
    *,
    refresh_receipt_path: Path,
    case_id: str,
    plan_revision_id: str,
    recurrence_after: str,
) -> dict[str, Any]:
    """Prove recurrence from two later canonical-case shadow snapshots.

    A planner command cannot manufacture this proof.  The receipt must come from the
    centralized refresh workflow, retain both cycle receipts and their case-registry
    snapshots, and show the exact plan's evidence baseline remained unchanged.
    """

    path = refresh_receipt_path.expanduser().resolve()
    receipt = _load_json_object(path, error="outcome_recurrence_refresh_receipt_invalid")
    receipt_schema = receipt.get("schema_version")
    if receipt_schema not in {3, 4} or receipt.get("producer") != (
        "usertest_implement.backlog_refresh"
    ):
        raise ValueError("outcome_recurrence_refresh_receipt_identity_invalid")
    content_hash = receipt.get("receipt_content_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_content_sha256"}
    if content_hash != _sha256_json(unsigned):
        raise ValueError("outcome_recurrence_refresh_receipt_hash_mismatch")
    after = _utc_timestamp(recurrence_after, field="recurrence_after")
    if _utc_timestamp(receipt.get("recorded_at_utc"), field="refresh_recorded_at") <= after:
        raise ValueError("outcome_recurrence_refresh_not_later_than_outcome")

    refresh_scope = path.parent
    state_path = Path(
        _required_text(receipt.get("shadow_state_path"), field="shadow_state_path")
    ).expanduser().resolve()
    if not state_path.is_relative_to(refresh_scope):
        raise ValueError("outcome_recurrence_shadow_state_outside_refresh_scope")
    state_hash = _required_sha256(
        receipt.get("shadow_state_sha256"), field="shadow_state_sha256"
    )
    if not state_path.is_file() or _sha256_file(state_path) != state_hash:
        raise ValueError("outcome_recurrence_shadow_state_changed")
    state = _load_json_object(state_path, error="outcome_recurrence_shadow_state_invalid")
    if state.get("ready_for_export") is not True or int(
        state.get("consecutive_stable_passes") or 0
    ) < 2:
        raise ValueError("outcome_recurrence_shadow_gate_not_open")

    if receipt_schema == 4:
        if receipt.get("activation_mode") != "operational_bound":
            raise ValueError("outcome_recurrence_operational_activation_required")
        anchor_ids = receipt.get("release_anchor_cycle_ids")
        if (
            not isinstance(anchor_ids, list)
            or len(anchor_ids) < 2
            or state.get("release_anchor_cycle_ids") != anchor_ids
        ):
            raise ValueError("outcome_recurrence_release_anchor_invalid")
        ids = receipt.get("observation_cycle_ids")
        cycles = receipt.get("observation_cycles")
    else:
        ids = receipt.get("qualifying_cycle_ids")
        cycles = receipt.get("qualifying_cycles")
    if (
        not isinstance(ids, list)
        or len(ids) != 2
        or len(set(ids)) != 2
        or not isinstance(cycles, list)
        or len(cycles) != 2
        or [cycle.get("cycle_id") if isinstance(cycle, dict) else None for cycle in cycles]
        != ids
    ):
        raise ValueError("outcome_recurrence_two_fresh_cycles_required")
    state_cycles_raw = state.get("cycles")
    state_cycles = state_cycles_raw if isinstance(state_cycles_raw, list) else []
    state_observation_cycles = (
        [
            cycle
            for cycle in state_cycles
            if isinstance(cycle, dict) and cycle.get("cycle_mode") == "operational"
        ]
        if receipt_schema == 4
        else state_cycles
    )
    if [
        cycle.get("cycle_id") if isinstance(cycle, dict) else None
        for cycle in state_observation_cycles[-2:]
    ] != ids:
        raise ValueError("outcome_recurrence_cycles_not_latest")

    case_projections: list[dict[str, Any]] = []
    verified_cycles: list[dict[str, Any]] = []
    new_source_runs: dict[str, dict[str, Any]] = {}
    for index, cycle in enumerate(cycles):
        if not isinstance(cycle, dict) or cycle.get("passed") is not True:
            raise ValueError(f"outcome_recurrence_cycle_not_passed:{index}")
        generated_at = _utc_timestamp(
            cycle.get("generated_at"), field=f"qualifying_cycles[{index}].generated_at"
        )
        if generated_at <= after:
            raise ValueError(f"outcome_recurrence_cycle_not_later:{index}")
        cycle_path = Path(
            _required_text(
                cycle.get("cycle_receipt_path"),
                field=f"qualifying_cycles[{index}].cycle_receipt_path",
            )
        ).expanduser().resolve()
        if not cycle_path.is_relative_to(refresh_scope):
            raise ValueError(f"outcome_recurrence_cycle_receipt_outside_scope:{index}")
        cycle_hash = _required_sha256(
            cycle.get("cycle_receipt_sha256"),
            field=f"qualifying_cycles[{index}].cycle_receipt_sha256",
        )
        if not cycle_path.is_file() or _sha256_file(cycle_path) != cycle_hash:
            raise ValueError(f"outcome_recurrence_cycle_receipt_changed:{index}")
        cycle_receipt = _load_json_object(
            cycle_path, error=f"outcome_recurrence_cycle_receipt_invalid:{index}"
        )
        if (
            cycle_receipt.get("cycle_id") != ids[index]
            or cycle_receipt.get("generated_at") != cycle.get("generated_at")
            or cycle_receipt.get("passed") is not True
            or (receipt_schema == 4 and cycle_receipt.get("cycle_mode") != "operational")
        ):
            raise ValueError(f"outcome_recurrence_cycle_identity_mismatch:{index}")

        registry_path = Path(
            _required_text(
                cycle.get("case_registry_snapshot_path"),
                field=f"qualifying_cycles[{index}].case_registry_snapshot_path",
            )
        ).expanduser().resolve()
        if not registry_path.is_relative_to(refresh_scope):
            raise ValueError(f"outcome_recurrence_case_registry_outside_scope:{index}")
        registry_hash = _required_sha256(
            cycle.get("case_registry_sha256"),
            field=f"qualifying_cycles[{index}].case_registry_sha256",
        )
        if not registry_path.is_file() or _sha256_file(registry_path) != registry_hash:
            raise ValueError(f"outcome_recurrence_case_registry_changed:{index}")
        artifact_receipts = cycle_receipt.get("artifact_receipts")
        registry_receipt = next(
            (
                row
                for row in artifact_receipts
                if isinstance(row, dict) and row.get("name") == "case_registry"
            ),
            None,
        ) if isinstance(artifact_receipts, list) else None
        if not isinstance(registry_receipt, dict) or any(
            registry_receipt.get(key) != cycle.get(receipt_key)
            for key, receipt_key in (
                ("snapshot_path", "case_registry_snapshot_path"),
                ("sha256", "case_registry_sha256"),
                ("content_sha256", "case_registry_content_sha256"),
            )
        ):
            raise ValueError(f"outcome_recurrence_case_registry_receipt_mismatch:{index}")
        registry = _load_json_object(
            registry_path, error=f"outcome_recurrence_case_registry_invalid:{index}"
        )
        atoms_path = Path(
            _required_text(
                cycle.get("atoms_snapshot_path"),
                field=f"qualifying_cycles[{index}].atoms_snapshot_path",
            )
        ).expanduser().resolve()
        if not atoms_path.is_relative_to(refresh_scope):
            raise ValueError(f"outcome_recurrence_atoms_snapshot_outside_scope:{index}")
        atoms_hash = _required_sha256(
            cycle.get("atoms_sha256"),
            field=f"qualifying_cycles[{index}].atoms_sha256",
        )
        if not atoms_path.is_file() or _sha256_file(atoms_path) != atoms_hash:
            raise ValueError(f"outcome_recurrence_atoms_snapshot_changed:{index}")
        atoms_receipt = next(
            (
                row
                for row in artifact_receipts
                if isinstance(row, dict) and row.get("name") == "atoms"
            ),
            None,
        ) if isinstance(artifact_receipts, list) else None
        if not isinstance(atoms_receipt, dict) or any(
            atoms_receipt.get(key) != cycle.get(receipt_key)
            for key, receipt_key in (
                ("snapshot_path", "atoms_snapshot_path"),
                ("sha256", "atoms_sha256"),
                ("content_sha256", "atoms_content_sha256"),
            )
        ):
            raise ValueError(f"outcome_recurrence_atoms_receipt_mismatch:{index}")
        observed_window = _source_observation_window(atoms_path)
        if cycle.get("source_observation_window") != observed_window:
            raise ValueError(f"outcome_recurrence_source_window_changed:{index}")
        for run in observed_window["runs"]:
            timestamp = run.get("latest_timestamp_utc")
            if isinstance(timestamp, str) and _utc_timestamp(
                timestamp,
                field=f"qualifying_cycles[{index}].source_run_timestamp",
            ) > after:
                new_source_runs[str(run["run_rel"])] = dict(run)
        cases = registry.get("cases")
        case = cases.get(case_id) if isinstance(cases, dict) else None
        if not isinstance(case, dict):
            raise ValueError(f"outcome_recurrence_canonical_case_missing:{index}")
        revisions = case.get("plan_revisions")
        plan = revisions.get(plan_revision_id) if isinstance(revisions, dict) else None
        if not isinstance(plan, dict):
            raise ValueError(f"outcome_recurrence_plan_baseline_missing:{index}")
        baseline_ids = plan.get("source_evidence_atom_ids_at_plan")
        current_ids = case.get("evidence_atom_ids")
        baseline_revision = plan.get("case_revision_at_plan")
        current_revision = case.get("case_revision")
        derived_ids_raw = case.get("derived_evidence_atom_ids")
        derived_ids = set(derived_ids_raw) if isinstance(derived_ids_raw, list) else set()
        current_source_ids = (
            [atom_id for atom_id in current_ids if atom_id not in derived_ids]
            if isinstance(current_ids, list)
            else None
        )
        if (
            not isinstance(baseline_ids, list)
            or current_source_ids is None
            or sorted(set(current_source_ids)) != sorted(set(baseline_ids))
            or isinstance(baseline_revision, bool)
            or not isinstance(baseline_revision, int)
            or isinstance(current_revision, bool)
            or not isinstance(current_revision, int)
        ):
            raise ValueError(f"outcome_recurrence_new_case_evidence_detected:{index}")
        reopen = case.get("recurrence_reopen")
        lifecycle = case.get("current_lifecycle")
        reference = lifecycle.get("outcome_reference") if isinstance(lifecycle, dict) else None
        if (
            reopen is not None
            or not isinstance(reference, dict)
            or reference.get("plan_revision_id") != plan_revision_id
            or reference.get("source") == "same_class_recurrence_reopen"
            or case.get("state") == "unverified"
        ):
            raise ValueError(f"outcome_recurrence_case_reopened_or_unbound:{index}")
        projection = {
            "case_id": case_id,
            "case_revision": current_revision,
            "source_evidence_atom_ids": sorted(set(current_source_ids)),
            "plan_revision_id": plan_revision_id,
        }
        case_projections.append(projection)
        verified_cycles.append(
            {
                "cycle_id": ids[index],
                "generated_at": cycle.get("generated_at"),
                "cycle_receipt_path": str(cycle_path),
                "cycle_receipt_sha256": cycle_hash,
                "case_registry_snapshot_path": str(registry_path),
                "case_registry_sha256": registry_hash,
                "case_projection_sha256": _sha256_json(projection),
            }
        )
    if case_projections[0] != case_projections[1]:
        raise ValueError("outcome_recurrence_case_not_stable_across_cycles")
    if not new_source_runs:
        raise ValueError("outcome_recurrence_no_new_source_observation_window")
    return {
        "refresh_receipt_path": str(path),
        "refresh_receipt_sha256": _sha256_file(path),
        "refresh_receipt_content_sha256": content_hash,
        "recurrence_after": recurrence_after,
        "qualifying_cycles": verified_cycles,
        "case_projection": case_projections[-1],
        "new_source_observation_runs": [
            new_source_runs[key] for key in sorted(new_source_runs)
        ],
    }


def _workspace_git_argv(workspace: Path, *args: str) -> list[str]:
    """Build an exact, command-local Git trust grant for an isolated workspace."""

    resolved = workspace.expanduser().resolve()
    safe_dir = str(resolved).replace("\\", "/")
    return [
        "git",
        "-c",
        f"safe.directory={safe_dir}",
        "-C",
        str(resolved),
        *args,
    ]


def _resolved_head(workspace: Path) -> str:
    try:
        proc = subprocess.run(
            _workspace_git_argv(workspace, "rev-parse", "HEAD"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError("outcome_role_git_unavailable") from exc
    head = (proc.stdout or "").strip()
    if proc.returncode != 0 or len(head) != 40:
        raise ValueError("outcome_role_workspace_head_unavailable")
    return head.casefold()


def _resolve_commit(workspace: Path, commit: str) -> str:
    try:
        proc = subprocess.run(
            _workspace_git_argv(
                workspace,
                "rev-parse",
                "--verify",
                f"{commit}^{{commit}}",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError("outcome_role_git_unavailable") from exc
    resolved = (proc.stdout or "").strip()
    if proc.returncode != 0 or len(resolved) != 40:
        raise ValueError("outcome_role_merged_commit_unavailable")
    return resolved.casefold()


def _require_ancestor(workspace: Path, *, ancestor: str, descendant: str, field: str) -> None:
    proc = subprocess.run(
        _workspace_git_argv(
            workspace,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(f"outcome_role_commit_not_ancestor:{field}")


def _normalized_causal_proof_receipts(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("outcome_role_causal_proof_receipts_invalid")
    normalized: dict[str, dict[str, Any]] = {}
    for index, receipt in enumerate(raw):
        if not isinstance(receipt, dict):
            raise ValueError(f"outcome_role_causal_proof_receipt_invalid:{index}")
        proof_id = receipt.get("proof_receipt_id")
        proof_projection = {
            key: value for key, value in receipt.items() if key != "proof_receipt_id"
        }
        source_root = receipt.get("source_root")
        source_projection = (
            {
                key: value
                for key, value in source_root.items()
                if key != "source_root_sha256"
            }
            if isinstance(source_root, Mapping)
            else {}
        )
        positive_basis = (
            source_root.get("positive_basis") if isinstance(source_root, Mapping) else None
        )
        basis_projection = (
            {
                key: value for key, value in positive_basis.items() if key != "basis_sha256"
            }
            if isinstance(positive_basis, Mapping)
            else {}
        )
        observations = receipt.get("observations")
        baseline = observations.get("baseline") if isinstance(observations, Mapping) else None
        challenge = observations.get("challenge") if isinstance(observations, Mapping) else None
        intervention = receipt.get("intervention")
        positive = receipt.get("positive_outcome")
        replay_inputs = receipt.get("replay_inputs")
        replay_inputs_projection = (
            {
                key: value
                for key, value in replay_inputs.items()
                if key != "replay_inputs_sha256"
            }
            if isinstance(replay_inputs, Mapping)
            else {}
        )
        replay = receipt.get("replay_observation")
        replay_projection = (
            {
                key: value
                for key, value in replay.items()
                if key != "replay_observation_sha256"
            }
            if isinstance(replay, Mapping)
            else {}
        )
        predicate = positive.get("predicate") if isinstance(positive, Mapping) else None
        predicate_kind = predicate.get("kind") if isinstance(predicate, Mapping) else None
        evaluator = _CAUSAL_PREDICATE_EVALUATORS.get(str(predicate_kind))
        recorded_passed, recorded_error = (
            evaluator(predicate, positive.get("observed"))
            if evaluator is not None and isinstance(positive, Mapping)
            else (False, "predicate_unavailable")
        )
        expected_intervention_id = (
            "intervention:"
            + _sha256_json(
                {
                    "source_root_sha256": source_root.get("source_root_sha256"),
                    "baseline_observation_sha256": baseline.get("observation_sha256"),
                    "challenge_observation_sha256": challenge.get("observation_sha256"),
                    "intervention": {
                        key: value
                        for key, value in intervention.items()
                        if key not in {"intervention_id", "adapter_evidence"}
                    },
                }
            )
            if isinstance(source_root, Mapping)
            and isinstance(baseline, Mapping)
            and isinstance(challenge, Mapping)
            and isinstance(intervention, Mapping)
            else None
        )
        if (
            receipt.get("schema_version") != 1
            or not isinstance(proof_id, str)
            or proof_id != f"causal_proof:{_sha256_json(proof_projection)}"
            or proof_id in normalized
            or not isinstance(receipt.get("adapter_id"), str)
            or not receipt.get("adapter_id", "").strip()
            or not isinstance(receipt.get("adapter_version"), str)
            or not receipt.get("adapter_version", "").strip()
            or not isinstance(source_root, Mapping)
            or source_root.get("runner_attested") is not True
            or source_root.get("source_root_sha256") != _sha256_json(source_projection)
            or not isinstance(positive_basis, Mapping)
            or positive_basis.get("runner_attested") is not True
            or positive_basis.get("basis_sha256") != _sha256_json(basis_projection)
            or positive_basis.get("predicate_sha256") != _sha256_json(predicate)
            or not isinstance(baseline, Mapping)
            or not isinstance(challenge, Mapping)
            or baseline.get("runner_attested") is not True
            or challenge.get("runner_attested") is not True
            or baseline.get("observation_sha256")
            != _sha256_json(
                {
                    key: value
                    for key, value in baseline.items()
                    if key != "observation_sha256"
                }
            )
            or challenge.get("observation_sha256")
            != _sha256_json(
                {
                    key: value
                    for key, value in challenge.items()
                    if key != "observation_sha256"
                }
            )
            or not isinstance(intervention, Mapping)
            or intervention.get("baseline_experiment_id") != baseline.get("experiment_id")
            or intervention.get("challenge_experiment_id") != challenge.get("experiment_id")
            or receipt.get("intervention_id") != expected_intervention_id
            or not isinstance(positive, Mapping)
            or positive.get("runner_evaluated") is not True
            or positive.get("passed") is not True
            or recorded_error is not None
            or recorded_passed is not True
            or not isinstance(replay_inputs, Mapping)
            or replay_inputs.get("schema_version") != 1
            or replay_inputs.get("runner_approved") is not True
            or replay_inputs.get("source_experiment_id") != baseline.get("experiment_id")
            or replay_inputs.get("replay_inputs_sha256")
            != _sha256_json(replay_inputs_projection)
            or not isinstance(replay_inputs.get("environment"), Mapping)
            or not isinstance(replay_inputs.get("disposable_state_paths"), list)
            or not isinstance(replay, Mapping)
            or replay.get("schema_version") != 1
            or replay.get("runner_attested") is not True
            or replay.get("source_experiment_id") != baseline.get("experiment_id")
            or replay.get("positive_reference_experiment_id")
            != challenge.get("experiment_id")
            or replay.get("predicate_input_mode")
            not in {
                "post_change_observation",
                "historical_baseline_and_post_change_observation",
            }
            or not isinstance(replay.get("selector"), Mapping)
            or not isinstance(replay.get("positive_reference_selector"), Mapping)
            or replay.get("selector", {}).get("source")
            not in _CAUSAL_OBSERVATION_READERS
            or replay.get("replay_observation_sha256") != _sha256_json(replay_projection)
        ):
            raise ValueError(f"outcome_role_causal_proof_receipt_invalid:{index}")
        normalized[proof_id] = dict(receipt)
    return normalized


def _causal_positive_contract_valid(
    contract: Mapping[str, Any],
    *,
    oracle: Mapping[str, Any],
    proofs: Mapping[str, Mapping[str, Any]],
) -> bool:
    proof_id = contract.get("proof_receipt_id")
    proof = proofs.get(str(proof_id))
    if not isinstance(proof, Mapping):
        return False
    intervention = proof.get("intervention")
    observations = proof.get("observations")
    baseline = observations.get("baseline") if isinstance(observations, Mapping) else None
    challenge = observations.get("challenge") if isinstance(observations, Mapping) else None
    positive = proof.get("positive_outcome")
    source_root = proof.get("source_root")
    basis = source_root.get("positive_basis") if isinstance(source_root, Mapping) else None
    adapter_contract = contract.get("adapter_contract")
    expected_postcondition = {
        "type": "causal_proof_predicate",
        "proof_receipt_id": proof_id,
        "intervention_id": proof.get("intervention_id"),
        "adapter_id": proof.get("adapter_id"),
        "adapter_version": proof.get("adapter_version"),
        "predicate": positive.get("predicate") if isinstance(positive, Mapping) else None,
        "observation_source": (
            positive.get("observation_source") if isinstance(positive, Mapping) else None
        ),
        "positive_basis_sha256": (
            basis.get("basis_sha256") if isinstance(basis, Mapping) else None
        ),
    }
    return bool(
        oracle.get("kind") == "causal_proof_replay"
        and proof_id in oracle.get("proof_receipt_ids", [])
        and isinstance(intervention, Mapping)
        and intervention.get("baseline_experiment_id") == oracle.get("research_experiment_id")
        and isinstance(baseline, Mapping)
        and isinstance(challenge, Mapping)
        and isinstance(adapter_contract, Mapping)
        and adapter_contract.get("adapter_id") == proof.get("adapter_id")
        and adapter_contract.get("adapter_version") == proof.get("adapter_version")
        and adapter_contract.get("baseline_observation_sha256")
        == baseline.get("observation_sha256")
        and adapter_contract.get("challenge_observation_sha256")
        == challenge.get("observation_sha256")
        and adapter_contract.get("adapter_evidence_sha256")
        == _sha256_json(proof.get("adapter_evidence"))
        and contract.get("positive_basis") == basis
        and contract.get("semantic_review_required")
        is (isinstance(basis, Mapping) and basis.get("semantic_review_required") is True)
        and contract.get("postconditions") == [expected_postcondition]
    )


def _validated_stage5_outcome_contract(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    contract_id = raw.get("outcome_contract_id")
    projection = {
        key: value for key, value in raw.items() if key != "outcome_contract_id"
    }
    if (
        contract_id != f"stage5_outcome_contract:{_sha256_json(projection)}"
        or raw.get("kind") != "selected_option_outcome_strategy"
        or raw.get("outcome_contract_status") != "approved_for_planning"
        or raw.get("post_change_evidence_status") != "unverified"
        or not isinstance(raw.get("strategy"), dict)
        or not isinstance(raw.get("review"), dict)
    ):
        return None
    return dict(raw)


def _is_fail_first_planned_replay(raw: Any) -> bool:
    if (
        not isinstance(raw, dict)
        or raw.get("kind") != "staged_replay"
        or raw.get("scenario_kind") != "fail_first_contract"
        or raw.get("positive_outcome_contracts") not in (None, [])
        or _validated_stage5_outcome_contract(raw.get("selected_outcome_contract"))
        is None
    ):
        return False
    baseline = raw.get("baseline")
    baseline_exit = baseline.get("exit_code") if isinstance(baseline, dict) else None
    if not (
        isinstance(baseline_exit, int)
        and not isinstance(baseline_exit, bool)
        and baseline_exit != 0
    ):
        return False
    return _valid_retained_stage3_fail_first_source(raw)


def _valid_retained_stage3_fail_first_source(raw: dict[str, Any]) -> bool:
    """Validate the reversible Stage-3 source retained by new fail-first plans."""

    retained = raw.get("retained_stage3_oracle")
    source_ref = raw.get("stage5_fail_first_source")
    if retained is None and source_ref is None:
        return True
    if not isinstance(retained, dict) or not isinstance(source_ref, dict):
        return False
    if (
        retained.get("retained_stage3_oracle") is not None
        or retained.get("stage5_fail_first_source") is not None
        or retained.get("selected_outcome_contract") is not None
    ):
        return False
    retained_id = retained.get("outcome_oracle_id")
    retained_projection = {
        key: value for key, value in retained.items() if key != "outcome_oracle_id"
    }
    retained_scenario = retained.get("scenario_kind")
    retained_baseline = retained.get("baseline")
    retained_exit = (
        retained_baseline.get("exit_code")
        if isinstance(retained_baseline, dict)
        else None
    )
    if (
        retained_id != f"outcome_oracle:{_sha256_json(retained_projection)}"
        or retained.get("schema_version") != 1
        or retained.get("kind") != "staged_replay"
        or retained.get("proof_scope") != "behavioral"
        or retained_scenario
        not in {
            "original_replay",
            "faithful_replay",
            "live_runtime",
            "fail_first_contract",
        }
        or isinstance(retained_exit, bool)
        or not isinstance(retained_exit, int)
        or retained_exit == 0
    ):
        return False
    retained_contracts_raw = retained.get("positive_outcome_contracts")
    if retained_contracts_raw is None:
        retained_contracts: list[dict[str, Any]] = []
    elif isinstance(retained_contracts_raw, list) and all(
        isinstance(value, dict) for value in retained_contracts_raw
    ):
        retained_contracts = retained_contracts_raw
    else:
        return False
    if retained_scenario == "fail_first_contract" and retained_contracts:
        return False
    retained_contract_ids = [
        str(contract["positive_outcome_contract_id"])
        for contract in retained_contracts
        if isinstance(contract.get("positive_outcome_contract_id"), str)
        and contract["positive_outcome_contract_id"]
    ]
    if (
        len(retained_contract_ids) != len(retained_contracts)
        or len(retained_contract_ids) != len(set(retained_contract_ids))
    ):
        return False
    retained_contract_ids.sort()
    if source_ref != {
        "schema_version": 1,
        "kind": "verified_stage3_fail_first_source",
        "source_outcome_oracle_id": retained_id,
        "source_scenario_kind": retained_scenario,
        "source_positive_outcome_contract_ids": retained_contract_ids,
    }:
        return False
    source_reversible = {
        key: value
        for key, value in retained.items()
        if key not in {"outcome_oracle_id", "positive_outcome_contracts", "scenario_kind"}
    }
    derived_reversible = {
        key: value
        for key, value in raw.items()
        if key
        not in {
            "outcome_oracle_id",
            "positive_outcome_contracts",
            "scenario_kind",
            "selected_outcome_contract",
            "retained_stage3_oracle",
            "stage5_fail_first_source",
        }
    }
    if source_reversible != derived_reversible:
        return False
    if retained_contracts:
        try:
            _normalize_outcome_oracle(retained)
        except ValueError:
            return False
    return True


def _normalize_outcome_oracle(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("outcome_role_oracle_invalid")
    oracle_id = raw.get("outcome_oracle_id")
    projection = {key: value for key, value in raw.items() if key != "outcome_oracle_id"}
    if oracle_id != f"outcome_oracle:{_sha256_json(projection)}":
        raise ValueError("outcome_role_oracle_hash_invalid")
    kind = raw.get("kind")
    positive_contracts = raw.get("positive_outcome_contracts")
    if kind == "stage5_planned_outcome":
        if (
            positive_contracts not in (None, [])
            or raw.get("proof_scope") != "planned_post_change_verification"
            or _validated_stage5_outcome_contract(
                raw.get("selected_outcome_contract")
            )
            is None
        ):
            raise ValueError("outcome_role_stage5_planned_oracle_invalid")
    elif kind == "staged_replay" and raw.get("scenario_kind") == (
        "fail_first_contract"
    ):
        if not _is_fail_first_planned_replay(raw):
            raise ValueError("outcome_role_fail_first_planned_oracle_invalid")
    else:
        if raw.get("selected_outcome_contract") is not None:
            raise ValueError("outcome_role_selected_outcome_contract_unexpected")
        if not isinstance(positive_contracts, list) or not positive_contracts:
            raise ValueError("outcome_role_positive_contract_missing")
        for index, contract in enumerate(positive_contracts):
            if not isinstance(contract, dict):
                raise ValueError(f"outcome_role_positive_contract_invalid:{index}")
            contract_id = contract.get("positive_outcome_contract_id")
            contract_projection = {
                key: value
                for key, value in contract.items()
                if key != "positive_outcome_contract_id"
            }
            postconditions = contract.get("postconditions")
            if (
                contract_id
                != f"positive_outcome_contract:{_sha256_json(contract_projection)}"
                or contract.get("kind")
                not in {
                    "repository_test_assertion",
                    "retained_research_harness_assertion",
                    "origin_evidence_semantic_contract",
                    "causal_proof_predicate",
                }
                or not isinstance(postconditions, list)
                or not postconditions
                or any(not isinstance(value, dict) for value in postconditions)
            ):
                raise ValueError(f"outcome_role_positive_contract_invalid:{index}")
    if kind in {"staged_replay", "causal_proof_replay"}:
        execution = raw.get("execution")
        argv = execution.get("argv") if isinstance(execution, dict) else None
        authorization = (
            execution.get("command_authorization")
            if isinstance(execution, dict)
            else None
        )
        if (
            raw.get("proof_scope")
            != (
                "behavioral"
                if kind == "staged_replay"
                else "adapter_causal_behavior"
            )
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(token, str) or not token for token in argv)
            or execution.get("shell") is not False
            or not isinstance(authorization, dict)
            or not isinstance(authorization.get("authorization_kind"), str)
            or not authorization.get("authorization_kind", "").strip()
            or authorization.get("executed_argv_sha256") != _sha256_json(argv)
            or authorization.get("shell") is not False
            or authorization.get("workspace_confined") is not True
        ):
            raise ValueError("outcome_role_replay_oracle_invalid")
        if kind == "causal_proof_replay":
            proof_ids = raw.get("proof_receipt_ids")
            setup = execution.get("replay_setup_receipt")
            setup_reference = execution.get("replay_setup_reference")
            setup_projection = (
                {
                    key: value
                    for key, value in setup.items()
                    if key != "replay_setup_sha256"
                }
                if isinstance(setup, Mapping)
                else {}
            )
            if (
                not isinstance(proof_ids, list)
                or not proof_ids
                or proof_ids != sorted(set(proof_ids))
                or not isinstance(setup, Mapping)
                or setup.get("runner_applied") is not True
                or setup.get("replay_setup_sha256") != _sha256_json(setup_projection)
                or not isinstance(setup_reference, Mapping)
                or setup_reference.get("source") != "research_experiment"
                or setup_reference.get("experiment_id")
                != raw.get("research_experiment_id")
                or not isinstance(setup_reference.get("replay_setup_sha256"), str)
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(setup_reference.get("replay_setup_sha256")),
                )
                is None
            ):
                raise ValueError("outcome_role_causal_replay_oracle_invalid")
    elif kind == "stage5_planned_outcome":
        pass
    elif kind == "config_state":
        targets = raw.get("state_targets")
        if (
            raw.get("proof_scope") != "configuration_state"
            or not isinstance(targets, list)
            or not targets
        ):
            raise ValueError("outcome_role_config_oracle_invalid")
    elif kind == "multi_scenario":
        scenarios = raw.get("scenarios")
        outer_contract_ids = {
            contract.get("positive_outcome_contract_id")
            for contract in positive_contracts
            if isinstance(contract, dict)
        }
        if (
            raw.get("proof_scope") != "multi_scenario"
            or not isinstance(scenarios, list)
            or len(scenarios) < 2
        ):
            raise ValueError("outcome_role_multi_scenario_oracle_invalid")
        observed_contract_ids: set[str] = set()
        for index, scenario in enumerate(scenarios):
            if not isinstance(scenario, dict):
                raise ValueError(f"outcome_role_multi_scenario_invalid:{index}")
            scenario_id = scenario.get("scenario_id")
            scenario_projection = {
                key: value for key, value in scenario.items() if key != "scenario_id"
            }
            child_raw = scenario.get("oracle")
            child = _normalize_outcome_oracle(child_raw)
            selected_id = scenario.get("positive_outcome_contract_id")
            child_contract_ids = {
                contract.get("positive_outcome_contract_id")
                for contract in child.get("positive_outcome_contracts", [])
                if isinstance(contract, dict)
            }
            predicates = scenario.get("predicates")
            if (
                scenario_id != f"outcome_scenario:{_sha256_json(scenario_projection)}"
                or child.get("kind") == "multi_scenario"
                or not isinstance(selected_id, str)
                or selected_id not in child_contract_ids
                or selected_id not in outer_contract_ids
                or selected_id in observed_contract_ids
                or not isinstance(predicates, list)
                or not predicates
                or any(not isinstance(value, dict) for value in predicates)
            ):
                raise ValueError(f"outcome_role_multi_scenario_invalid:{index}")
            observed_contract_ids.add(selected_id)
        if observed_contract_ids != outer_contract_ids:
            raise ValueError("outcome_role_multi_scenario_contract_coverage_invalid")
    else:
        raise ValueError("outcome_role_oracle_kind_invalid")
    return dict(raw)


class _ReachableScope(ast.NodeVisitor):
    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.calls: list[ast.Call] = []
        self.loaded_names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.loaded_names.add(node.id)


def _dotted_expression(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_expression(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _module_test_functions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[f"{node.name}.{member.name}"] = member
    return functions


def _reachable_functions(
    *,
    selected_name: str,
    selected: ast.FunctionDef | ast.AsyncFunctionDef,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    pending = [(selected_name, selected)]
    reached: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    while pending:
        name, node = pending.pop(0)
        if name in reached:
            continue
        reached[name] = node
        visitor = _ReachableScope(node)
        visitor.visit(node)
        class_name = name.rsplit(".", 1)[0] if "." in name else None
        for call in visitor.calls:
            dotted = _dotted_expression(call.func)
            helper = dotted if dotted in functions else None
            if helper is None and class_name is not None and dotted is not None:
                for prefix in ("self.", "cls."):
                    if dotted.startswith(prefix):
                        candidate = f"{class_name}.{dotted.removeprefix(prefix)}"
                        helper = candidate if candidate in functions else None
                        break
            if helper is not None and helper not in reached:
                pending.append((helper, functions[helper]))
    return sorted(reached.items())


def _reachable_contracts(
    reachable: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
) -> list[dict[str, str]]:
    return [
        {
            "function": name,
            "function_ast_sha256": hashlib.sha256(
                ast.dump(node, annotate_fields=True, include_attributes=False).encode()
            ).hexdigest(),
        }
        for name, node in reachable
    ]


def _relevant_imports_projection(
    tree: ast.Module,
    reachable: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
) -> list[dict[str, Any]]:
    loaded: set[str] = set()
    for _, node in reachable:
        visitor = _ReachableScope(node)
        visitor.visit(node)
        loaded.update(visitor.loaded_names)
    projection: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [
                ast.dump(alias, annotate_fields=True, include_attributes=False)
                for alias in node.names
                if (alias.asname or alias.name.split(".", 1)[0]) in loaded
            ]
            if names:
                projection.append({"kind": "Import", "names": names})
        elif isinstance(node, ast.ImportFrom):
            names = [
                ast.dump(alias, annotate_fields=True, include_attributes=False)
                for alias in node.names
                if (alias.asname or alias.name) in loaded
            ]
            if names:
                projection.append(
                    {
                        "kind": "ImportFrom",
                        "level": node.level,
                        "module": node.module,
                        "names": names,
                    }
                )
    return projection


def _verify_positive_contract_sources(
    oracle: dict[str, Any],
    *,
    workspace: Path,
    selected_contract_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Revalidate immutable repository contracts at the implementation head.

    A planned fail-first oracle keeps Stage-3 contracts as inert provenance rather
    than active success semantics.  Their repository sources still need revalidation,
    otherwise deriving the Stage-5 shape would silently remove tamper protection.
    """

    receipts: list[dict[str, Any]] = []
    contracts_raw = oracle.get("positive_outcome_contracts")
    if contracts_raw in (None, []):
        retained = oracle.get("retained_stage3_oracle")
        contracts_raw = (
            retained.get("positive_outcome_contracts", [])
            if isinstance(retained, dict)
            else []
        )
    for contract in contracts_raw if isinstance(contracts_raw, list) else []:
        if not isinstance(contract, dict):
            continue
        contract_id = contract.get("positive_outcome_contract_id")
        if selected_contract_ids is not None and contract_id not in selected_contract_ids:
            continue
        if contract.get("kind") == "retained_research_harness_assertion":
            semantic_basis = contract.get("semantic_basis")
            provenance = (
                semantic_basis.get("provenance")
                if isinstance(semantic_basis, dict)
                else None
            )
            if not isinstance(provenance, dict) or provenance.get("kind") != (
                "repository_contract_quote"
            ):
                continue
            path_raw = _required_text(provenance.get("path"), field="contract_path")
            relative = Path(path_raw)
            path = (workspace / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not path.is_relative_to(workspace)
                or not path.is_file()
                or path.is_symlink()
            ):
                raise ValueError("outcome_role_semantic_basis_source_changed")
            quote = _required_text(provenance.get("exact_quote"), field="exact_quote")
            try:
                content = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError("outcome_role_semantic_basis_source_invalid") from exc
            if quote not in content:
                raise ValueError("outcome_role_semantic_basis_source_changed")
            locator = provenance.get("contract_locator")
            if isinstance(locator, dict) and locator.get("kind") == "python_symbol":
                try:
                    tree = ast.parse(content)
                except SyntaxError as exc:
                    raise ValueError(
                        "outcome_role_semantic_basis_source_invalid"
                    ) from exc
                symbol = _required_text(locator.get("symbol"), field="contract_symbol")
                candidates = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(
                        node,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    )
                    and symbol.replace(":", ".").replace("#", ".").endswith(
                        node.name
                    )
                ]
                segment = (
                    ast.get_source_segment(content, candidates[0])
                    if len(candidates) == 1
                    else None
                )
                if not isinstance(segment, str) or quote not in segment:
                    raise ValueError("outcome_role_semantic_basis_source_changed")
            elif isinstance(locator, dict) and locator.get("kind") == (
                "mechanism_subject"
            ):
                subject = _required_text(locator.get("subject"), field="contract_subject")
                if subject not in quote:
                    raise ValueError("outcome_role_semantic_basis_source_changed")
            elif isinstance(locator, dict) and locator.get("kind") == "schema_pointer":
                suffix = path.suffix.casefold()
                format_name = (
                    "json"
                    if suffix == ".json"
                    else "toml"
                    if suffix == ".toml"
                    else "yaml"
                    if suffix in {".yaml", ".yml"}
                    else None
                )
                if format_name is None:
                    raise ValueError("outcome_role_semantic_basis_source_invalid")
                state = _read_config_state(
                    workspace,
                    {
                        "path": relative.as_posix(),
                        "format": format_name,
                        "json_pointer": locator.get("json_pointer"),
                    },
                )
                if (
                    state.get("exists") is not True
                    or state.get("value_sha256") != locator.get("value_sha256")
                ):
                    raise ValueError("outcome_role_semantic_basis_source_changed")
            else:
                raise ValueError("outcome_role_semantic_basis_source_invalid")
            receipts.append(
                {
                    "positive_outcome_contract_id": contract.get(
                        "positive_outcome_contract_id"
                    ),
                    "path": relative.as_posix(),
                    "expected_sha256": provenance.get("sha256"),
                    "observed_sha256": _sha256_file(path),
                    "status": "verified",
                }
            )
            continue
        if contract.get("kind") != "repository_test_assertion":
            continue
        repository = contract.get("repository_contract")
        if not isinstance(repository, dict):
            raise ValueError("outcome_role_repository_contract_invalid")
        path_raw = _required_text(repository.get("test_path"), field="test_path")
        relative = Path(path_raw)
        path = (workspace / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(workspace)
            or not path.is_file()
            or path.is_symlink()
        ):
            raise ValueError("outcome_role_repository_contract_source_missing")
        observed_sha256 = _sha256_file(path)
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
            tree = ast.parse(content)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            raise ValueError("outcome_role_repository_contract_source_invalid") from exc
        selector = _required_text(repository.get("selector"), field="selector")
        selector_parts = selector.split("::")
        nodes: list[ast.stmt] = tree.body
        selected: ast.FunctionDef | ast.AsyncFunctionDef | None = None
        for index, part in enumerate(selector_parts):
            match = next(
                (
                    node
                    for node in nodes
                    if isinstance(
                        node,
                        (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                    and node.name == part
                ),
                None,
            )
            if match is None:
                selected = None
                break
            if index == len(selector_parts) - 1:
                selected = (
                    match
                    if isinstance(match, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else None
                )
                break
            if not isinstance(match, ast.ClassDef):
                selected = None
                break
            nodes = match.body
        selected_name = ".".join(selector_parts)
        reachable = (
            _reachable_functions(
                selected_name=selected_name,
                selected=selected,
                functions=_module_test_functions(tree),
            )
            if selected is not None
            else []
        )
        source_segment = ast.get_source_segment(content, selected) if selected else None
        observed_function_sha256 = (
            hashlib.sha256(source_segment.encode("utf-8")).hexdigest()
            if isinstance(source_segment, str)
            else None
        )
        observed_reachable_contracts = _reachable_contracts(reachable)
        observed_imports_sha256 = _sha256_json(
            _relevant_imports_projection(tree, reachable)
        )
        if (
            observed_reachable_contracts
            != repository.get("reachable_function_contracts")
            or observed_imports_sha256
            != repository.get("relevant_module_imports_sha256")
        ):
            raise ValueError("outcome_role_repository_contract_source_changed")
        receipts.append(
            {
                "positive_outcome_contract_id": contract.get(
                    "positive_outcome_contract_id"
                ),
                "path": relative.as_posix(),
                "expected_sha256": repository.get("test_file_sha256"),
                "observed_sha256": observed_sha256,
                "observed_test_function_source_sha256": observed_function_sha256,
                "observed_reachable_function_contracts": observed_reachable_contracts,
                "observed_relevant_module_imports_sha256": observed_imports_sha256,
                "test_function_source_sha256": repository.get(
                    "test_function_source_sha256"
                ),
                "status": "verified",
            }
        )
    return receipts


def _normalize_role_contract(role: str, raw: Any) -> dict[str, Any]:
    if role not in OUTCOME_EVIDENCE_ROLES:
        raise ValueError(f"outcome_role_unknown:{role}")
    if not isinstance(raw, dict):
        raise ValueError(f"outcome_role_contract_invalid:{role}")
    supplied_hash = _required_sha256(
        raw.get("role_contract_sha256"),
        field="role_contract_sha256",
    )
    unsigned = {key: value for key, value in raw.items() if key != "role_contract_sha256"}
    if _sha256_json(unsigned) != supplied_hash:
        raise ValueError("outcome_role_contract_hash_mismatch")
    description = _required_text(unsigned.get("description"), field="description")
    oracle_raw = unsigned.get("oracle")
    if oracle_raw is not None and role != "original_scenario":
        raise ValueError("outcome_role_oracle_for_non_original_role")
    oracle = _normalize_outcome_oracle(oracle_raw) if oracle_raw is not None else None
    oracle_kind = oracle.get("kind") if oracle is not None else None
    verification_owner = unsigned.get("verification_owner")
    centralized_recurrence = (
        role == "recurrence"
        and verification_owner == _CENTRALIZED_RECURRENCE_OWNER
    )
    if verification_owner is not None and not centralized_recurrence:
        raise ValueError("outcome_role_verification_owner_invalid")
    causal_proofs = (
        _normalized_causal_proof_receipts(unsigned.get("causal_proof_receipts"))
        if oracle_kind == "causal_proof_replay"
        else {}
    )
    if oracle_kind == "causal_proof_replay":
        if set(causal_proofs) != set(oracle.get("proof_receipt_ids", [])):
            raise ValueError("outcome_role_causal_proof_receipt_coverage_invalid")
        causal_contracts = [
            contract
            for contract in oracle.get("positive_outcome_contracts", [])
            if isinstance(contract, Mapping)
            and contract.get("kind") == "causal_proof_predicate"
        ]
        if not causal_contracts or any(
            not _causal_positive_contract_valid(
                contract,
                oracle=oracle,
                proofs=causal_proofs,
            )
            for contract in causal_contracts
        ):
            raise ValueError("outcome_role_causal_positive_contract_invalid")
    elif unsigned.get("causal_proof_receipts") not in (None, []):
        raise ValueError("outcome_role_causal_proof_receipts_unexpected")
    planned_or_fail_first_oracle = bool(
        oracle_kind == "stage5_planned_outcome"
        or _is_fail_first_planned_replay(oracle)
    )
    if oracle is not None and not planned_or_fail_first_oracle:
        contract_ids = {
            str(contract.get("positive_outcome_contract_id"))
            for contract in oracle.get("positive_outcome_contracts", [])
            if isinstance(contract, dict)
        }
        selected_raw = unsigned.get("selected_positive_outcome_contract_ids")
        selected = (
            [value for value in selected_raw if isinstance(value, str) and value]
            if isinstance(selected_raw, list)
            else list(contract_ids)
            if len(contract_ids) == 1
            else []
        )
        if (
            not selected
            or len(selected) != len(set(selected))
            or any(value not in contract_ids for value in selected)
            or (oracle_kind == "multi_scenario" and set(selected) != contract_ids)
            or (oracle_kind != "multi_scenario" and len(selected) != 1)
        ):
            raise ValueError("outcome_role_selected_positive_contract_invalid")
    elif planned_or_fail_first_oracle and unsigned.get(
        "selected_positive_outcome_contract_ids"
    ) not in (None, []):
        raise ValueError(
            "outcome_role_selected_positive_contract_unexpected_for_planned_outcome"
        )
    if oracle is not None and unsigned.get("required_proof_scope") != oracle.get(
        "proof_scope"
    ):
        raise ValueError("outcome_role_oracle_scope_mismatch")
    commands_raw = unsigned.get("commands")
    if not isinstance(commands_raw, list) or (
        not commands_raw and not centralized_recurrence and oracle is None
    ):
        raise ValueError("outcome_role_commands_invalid")
    commands = [
        _required_text(command, field=f"commands[{index}]")
        for index, command in enumerate(commands_raw)
    ]
    if len(commands) != len(set(commands)):
        raise ValueError("outcome_role_commands_duplicate")
    if oracle is not None and oracle_kind != "stage5_planned_outcome" and commands:
        raise ValueError("outcome_role_oracle_commands_forbidden")
    if centralized_recurrence and commands:
        raise ValueError("outcome_role_centralized_recurrence_commands_invalid")
    for command in commands:
        errors = verification_command_safety_errors(command)
        if errors:
            raise ValueError(
                f"outcome_role_command_unsafe:{command!r}:" + ";".join(errors)
            )
    predicates = unsigned.get("predicates")
    if not isinstance(predicates, list) or (
        not predicates and not centralized_recurrence
    ) or any(
        not isinstance(predicate, dict) for predicate in predicates
    ):
        raise ValueError("outcome_role_predicates_invalid")
    if centralized_recurrence and predicates:
        raise ValueError("outcome_role_centralized_recurrence_predicates_invalid")
    if oracle_kind == "staged_replay" and not any(
        isinstance(predicate, dict)
        and predicate.get("type") == "command_exit_code"
        and predicate.get("command_index") == 0
        for predicate in predicates
    ):
        raise ValueError("outcome_role_oracle_exit_predicate_missing")
    if oracle_kind == "causal_proof_replay":
        expected_predicates = {
            _canonical_json(contract["postconditions"][0])
            for contract in oracle.get("positive_outcome_contracts", [])
            if isinstance(contract, Mapping)
            and contract.get("kind") == "causal_proof_predicate"
            and isinstance(contract.get("postconditions"), list)
            and len(contract["postconditions"]) == 1
        }
        observed_predicates = {
            _canonical_json(predicate)
            for predicate in predicates
            if predicate.get("type") == "causal_proof_predicate"
        }
        if observed_predicates != expected_predicates or len(predicates) != len(
            expected_predicates
        ):
            raise ValueError("outcome_role_causal_predicate_coverage_invalid")
    if oracle_kind == "config_state":
        target_ids = {
            str(target.get("target_id"))
            for target in oracle.get("state_targets", [])
            if isinstance(target, dict)
        }
        predicate_ids = {
            str(predicate.get("target_id"))
            for predicate in predicates
            if isinstance(predicate, dict)
            and predicate.get("type") == "oracle_state_equals"
        }
        if predicate_ids != target_ids:
            raise ValueError("outcome_role_oracle_state_coverage_invalid")
    if oracle_kind == "multi_scenario":
        scenario_ids = {
            str(scenario.get("scenario_id"))
            for scenario in oracle.get("scenarios", [])
            if isinstance(scenario, dict)
        }
        predicate_scenario_ids = {
            str(predicate.get("scenario_id"))
            for predicate in predicates
            if isinstance(predicate, dict)
            and predicate.get("type") == "oracle_scenario_passed"
        }
        if scenario_ids != predicate_scenario_ids or len(predicates) != len(scenario_ids):
            raise ValueError("outcome_role_multi_scenario_predicate_coverage_invalid")
    return {
        **unsigned,
        "description": description,
        "commands": commands,
        "predicates": [dict(predicate) for predicate in predicates],
        **({"oracle": oracle} if oracle is not None else {}),
        **(
            {"causal_proof_receipts": list(causal_proofs.values())}
            if causal_proofs
            else {}
        ),
        "role_contract_sha256": supplied_hash,
    }


def _command_argv(command: str) -> list[str]:
    if os.name == "nt":
        return [
            "powershell",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
    return ["/bin/sh", "-lc", command]


def _run_command(
    command: str,
    *,
    workspace: Path,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    timed_out = False
    try:
        proc = subprocess.run(
            _command_argv(command),
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code: int | None = int(proc.returncode)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        error = None
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
        error = "timed_out"
    except OSError as exc:
        exit_code = None
        stdout = ""
        stderr = ""
        error = f"{type(exc).__name__}: {exc}"
    return {
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "cancelled": False,
        "error": error,
    }


def _sanitized_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if _SENSITIVE_ENVIRONMENT_RE.search(key) is None
    } | {"CI": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _run_argv(
    argv: list[str],
    *,
    workspace: Path,
    timeout_seconds: float | None,
    environment_overrides: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    timed_out = False
    environment = _sanitized_environment()
    for key, value in (environment_overrides or {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        exit_code: int | None = int(proc.returncode)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        error = None
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = str(exc.stdout or "")
        stderr = str(exc.stderr or "")
        error = "timed_out"
    except OSError as exc:
        exit_code = None
        stdout = ""
        stderr = ""
        error = f"{type(exc).__name__}: {exc}"
    return {
        "command": " ".join(argv),
        "argv": list(argv),
        "shell": False,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "cancelled": False,
        "error": error,
    }


def _causal_replay_setup(
    execution: Mapping[str, Any],
    *,
    workspace: Path,
    causal_proofs: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, str | None], list[Path], list[str]]:
    receipt = execution.get("replay_setup_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("outcome_oracle_replay_setup_missing")
    receipt_environment = receipt.get("environment")
    receipt_environment = (
        receipt_environment if isinstance(receipt_environment, Mapping) else {}
    )
    proof_inputs = [
        proof.get("replay_inputs")
        for proof in causal_proofs.values()
        if isinstance(proof.get("replay_inputs"), Mapping)
    ]
    retained_inputs = (
        proof_inputs[0]
        if proof_inputs
        and len(proof_inputs) == len(causal_proofs)
        and all(value == proof_inputs[0] for value in proof_inputs)
        else None
    )
    retained_environment = (
        retained_inputs.get("environment")
        if isinstance(retained_inputs, Mapping)
        and isinstance(retained_inputs.get("environment"), Mapping)
        else {}
    )
    if isinstance(retained_inputs, Mapping):
        supplied_hash = retained_inputs.get("replay_inputs_sha256")
        projection = {
            key: value
            for key, value in retained_inputs.items()
            if key != "replay_inputs_sha256"
        }
        if (
            retained_inputs.get("schema_version") != 1
            or retained_inputs.get("runner_approved") is not True
            or supplied_hash != _sha256_json(projection)
        ):
            raise ValueError("outcome_oracle_replay_inputs_invalid")
    overrides: dict[str, str | None] = {}
    for key, variable_receipt in receipt_environment.items():
        if (
            not isinstance(key, str)
            or not key
            or _SENSITIVE_ENVIRONMENT_RE.search(key) is not None
            or not isinstance(variable_receipt, Mapping)
            or not isinstance(variable_receipt.get("present"), bool)
        ):
            raise ValueError("outcome_oracle_replay_environment_invalid")
        if variable_receipt.get("present") is False:
            overrides[key] = None
            continue
        value = retained_environment.get(key)
        if (
            not isinstance(value, str)
            or hashlib.sha256(value.encode("utf-8")).hexdigest()
            != variable_receipt.get("value_sha256")
        ):
            raise ValueError(f"outcome_oracle_replay_input_unavailable:{key}")
        overrides[key] = value
    if set(retained_environment) - set(receipt_environment):
        raise ValueError("outcome_oracle_replay_environment_unbound")

    paths_raw = receipt.get("disposable_state_paths")
    path_values = paths_raw if isinstance(paths_raw, list) else []
    if isinstance(retained_inputs, Mapping) and retained_inputs.get(
        "disposable_state_paths"
    ) != path_values:
        raise ValueError("outcome_oracle_replay_state_paths_mismatch")
    resolved: list[Path] = []
    normalized_paths: list[str] = []
    for index, path_raw in enumerate(path_values):
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise ValueError(f"outcome_oracle_replay_state_path_invalid:{index}")
        relative = Path(path_raw)
        path = (workspace / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.is_relative_to(workspace)
            or any(parent.is_symlink() for parent in [path, *path.parents] if parent != workspace)
            or path.exists()
        ):
            raise ValueError(f"outcome_oracle_replay_state_path_unsafe:{index}")
        resolved.append(path)
        normalized_paths.append(relative.as_posix())
    return overrides, resolved, normalized_paths


def _remove_causal_disposable_state(paths: list[Path], *, workspace: Path) -> None:
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        if not path.is_relative_to(workspace) or path.is_symlink():
            raise ValueError("outcome_oracle_replay_cleanup_unsafe")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        parent = path.parent
        while parent != workspace and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _causal_observation_results(
    proofs: Mapping[str, Mapping[str, Any]],
    *,
    commands: list[dict[str, Any]],
    workspace: Path,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for proof_id, proof in proofs.items():
        replay = proof.get("replay_observation")
        selector = replay.get("selector") if isinstance(replay, Mapping) else None
        source = selector.get("source") if isinstance(selector, Mapping) else None
        reader = _CAUSAL_OBSERVATION_READERS.get(str(source))
        if reader is None or not isinstance(selector, Mapping):
            results[proof_id] = {
                "proof_receipt_id": proof_id,
                "selector": selector,
                "observed": None,
                "error": f"causal_observation_source_unavailable:{source}",
            }
            continue
        observed_ok, fresh_observed, observation_error = reader(
            selector,
            commands,
            workspace,
        )
        observations = proof.get("observations")
        baseline = observations.get("baseline") if isinstance(observations, Mapping) else None
        mode = replay.get("predicate_input_mode") if isinstance(replay, Mapping) else None
        predicate_observed = (
            {
                "before": baseline.get("observed"),
                "after": fresh_observed,
            }
            if mode == "historical_baseline_and_post_change_observation"
            and isinstance(baseline, Mapping)
            else fresh_observed
        )
        results[proof_id] = {
            "proof_receipt_id": proof_id,
            "selector": dict(selector),
            "observed": predicate_observed,
            "fresh_observed": fresh_observed,
            "observed_sha256": _sha256_json(predicate_observed),
            "error": None if observed_ok else observation_error or "causal_observation_failed",
        }
    return results


def _workspace_status(workspace: Path) -> str:
    proc = subprocess.run(
        _workspace_git_argv(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError("outcome_role_workspace_status_unavailable")
    return proc.stdout or ""


def _workspace_file_manifest(workspace: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    root = workspace.resolve()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            manifest[relative.as_posix()] = {"kind": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            manifest[relative.as_posix()] = {
                "kind": "file",
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
    return manifest


def _verify_oracle_asset(
    asset: dict[str, Any],
    *,
    trusted_root: Path,
) -> tuple[Path, dict[str, Any]]:
    root = trusted_root.expanduser().resolve()
    relative_raw = _required_text(asset.get("runs_relative_path"), field="asset_path")
    relative = Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("outcome_oracle_asset_path_unsafe")
    source_root = (root / relative).resolve()
    if not source_root.is_relative_to(root) or not source_root.is_dir():
        raise ValueError("outcome_oracle_asset_missing")
    manifest = asset.get("manifest")
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("outcome_oracle_asset_manifest_invalid")
    if asset.get("manifest_sha256") != _sha256_json(manifest):
        raise ValueError("outcome_oracle_asset_manifest_hash_mismatch")
    expected_asset_id = "outcome_asset:" + _sha256_json(
        {"schema_version": 1, "manifest": manifest}
    )
    if asset.get("asset_id") != expected_asset_id:
        raise ValueError("outcome_oracle_asset_id_mismatch")
    observed: dict[str, Any] = {}
    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        relative_key = path.relative_to(source_root).as_posix()
        if path.is_symlink():
            raise ValueError(f"outcome_oracle_asset_entry_unsafe:{relative_key}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"outcome_oracle_asset_entry_unsafe:{relative_key}")
        observed[relative_key] = {
            "kind": "file",
            "mode": stat.S_IMODE(path.stat().st_mode),
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    if observed != manifest:
        raise ValueError("outcome_oracle_asset_hash_mismatch")
    return source_root, observed


def _materialize_oracle_asset(
    *,
    source_root: Path,
    manifest: dict[str, Any],
    workspace: Path,
) -> list[Path]:
    copied: list[Path] = []
    for relative_raw in sorted(manifest):
        relative = Path(relative_raw)
        source = (source_root / relative).resolve()
        destination = (workspace / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.as_posix().startswith(".usertest_research/")
            or not source.is_relative_to(source_root)
            or not destination.is_relative_to(workspace)
            or destination.exists()
        ):
            raise ValueError(f"outcome_oracle_materialization_collision:{relative_raw}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(destination)
    return copied


def _remove_materialized_asset(copied: list[Path], *, workspace: Path) -> None:
    for path in sorted(copied, key=lambda item: len(item.parts), reverse=True):
        if path.exists() and path.is_relative_to(workspace):
            path.unlink()
    research_root = (workspace / ".usertest_research").resolve()
    if research_root.exists() and research_root.is_relative_to(workspace):
        for directory in sorted(
            (path for path in research_root.rglob("*") if path.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            research_root.rmdir()
        except OSError:
            pass


def _json_pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    if pointer == "":
        return True, value
    cursor = value
    for raw_token in pointer.removeprefix("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(cursor, dict) and token in cursor:
            cursor = cursor[token]
        elif isinstance(cursor, list) and token.isdigit() and int(token) < len(cursor):
            cursor = cursor[int(token)]
        else:
            return False, None
    return True, cursor


def _read_config_state(workspace: Path, target: dict[str, Any]) -> dict[str, Any]:
    path_raw = _required_text(target.get("path"), field="config_path")
    relative = Path(path_raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("outcome_oracle_config_path_unsafe")
    source = (workspace / relative).resolve()
    if not source.is_relative_to(workspace) or not source.is_file() or source.is_symlink():
        raise ValueError("outcome_oracle_config_source_missing")
    try:
        content = source.read_text(encoding="utf-8")
        format_name = target.get("format")
        if format_name == "json":
            value = json.loads(content)
        elif format_name == "toml":
            value = tomllib.loads(content)
        elif format_name == "yaml":
            value = yaml.safe_load(content)
        else:
            raise ValueError("outcome_oracle_config_format_invalid")
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        yaml.YAMLError,
    ) as exc:
        raise ValueError("outcome_oracle_config_parse_failed") from exc
    pointer = target.get("json_pointer")
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("outcome_oracle_config_pointer_invalid")
    exists, observed = _json_pointer(value, pointer)
    try:
        value_sha256 = _sha256_json(observed) if exists else None
    except (TypeError, ValueError) as exc:
        raise ValueError("outcome_oracle_config_value_not_json_compatible") from exc
    return {
        "target_id": target.get("target_id"),
        "path": relative.as_posix(),
        "json_pointer": pointer,
        "source_file_sha256": _sha256_file(source),
        "exists": exists,
        "value": observed if exists else None,
        "value_sha256": value_sha256,
    }


def _snapshot_json_artifact(
    *,
    workspace: Path,
    output_dir: Path,
    predicate_index: int,
    path_raw: Any,
) -> tuple[dict[str, Any] | None, Any, str | None]:
    if not isinstance(path_raw, str) or not path_raw.strip():
        return None, None, "artifact_path_invalid"
    relative = Path(path_raw.strip())
    if relative.is_absolute() or ".." in relative.parts:
        return None, None, "artifact_path_unsafe"
    source = (workspace / relative).resolve()
    if not source.is_relative_to(workspace) or not source.is_file():
        return None, None, "artifact_missing"
    snapshot_dir = output_dir / "evidence_artifacts"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_dir / f"{predicate_index:03d}-{source.name}"
    shutil.copyfile(source, snapshot)
    try:
        value = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None, "artifact_json_invalid"
    receipt = {
        "source_relative_path": relative.as_posix(),
        "snapshot_path": str(snapshot),
        "snapshot_sha256": _sha256_file(snapshot),
    }
    return receipt, value, None


def _evaluate_predicates(
    predicates: list[dict[str, Any]],
    *,
    commands: list[dict[str, Any]],
    workspace: Path,
    output_dir: Path,
    oracle_states: dict[str, dict[str, Any]] | None = None,
    oracle_scenarios: list[dict[str, Any]] | None = None,
    causal_observations: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, predicate in enumerate(predicates):
        predicate_type = predicate.get("type")
        observed: Any = None
        artifact_receipt: dict[str, Any] | None = None
        error: str | None = None
        passed = False
        stream_predicate_types = {
            f"command_{source}_{operator}"
            for source in ("stdout", "stderr", "combined")
            for operator in ("contains", "not_contains", "equals")
        }
        if predicate_type == "command_exit_code" or predicate_type in stream_predicate_types:
            command_index = predicate.get("command_index")
            if (
                isinstance(command_index, bool)
                or not isinstance(command_index, int)
                or command_index < 0
                or command_index >= len(commands)
            ):
                error = "command_index_invalid"
            else:
                result = commands[command_index]
                if predicate_type == "command_exit_code":
                    observed = result.get("exit_code")
                    passed = (
                        result.get("timed_out") is False
                        and result.get("cancelled") is False
                        and observed == predicate.get("equals")
                    )
                else:
                    _, source, operator = str(predicate_type).split("_", 2)
                    observed = (
                        f"{result.get('stdout') or ''}{result.get('stderr') or ''}"
                        if source == "combined"
                        else result.get(source)
                    )
                    expected = predicate.get("value")
                    passed = (
                        isinstance(expected, str)
                        and expected != ""
                        and isinstance(observed, str)
                        and (
                            expected in observed
                            if operator == "contains"
                            else expected not in observed
                            if operator == "not_contains"
                            else expected == observed
                        )
                        and result.get("timed_out") is False
                        and result.get("cancelled") is False
                    )
        elif predicate_type == "oracle_scenario_passed":
            scenario_index = predicate.get("scenario_index")
            if (
                isinstance(scenario_index, bool)
                or not isinstance(scenario_index, int)
                or scenario_index < 0
                or not isinstance(oracle_scenarios, list)
                or scenario_index >= len(oracle_scenarios)
            ):
                error = "oracle_scenario_index_invalid"
            else:
                scenario = oracle_scenarios[scenario_index]
                observed = {
                    "scenario_id": scenario.get("scenario_id"),
                    "passed": scenario.get("passed"),
                }
                passed = (
                    scenario.get("scenario_id") == predicate.get("scenario_id")
                    and scenario.get("passed") is True
                    and scenario.get("execution_integrity") is True
                    and scenario.get("timed_out") is False
                    and scenario.get("cancelled") is False
                )
        elif predicate_type == "artifact_json_value":
            artifact_receipt, artifact, error = _snapshot_json_artifact(
                workspace=workspace,
                output_dir=output_dir,
                predicate_index=index,
                path_raw=predicate.get("path"),
            )
            if error is None:
                pointer = predicate.get("json_pointer")
                if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
                    error = "json_pointer_invalid"
                else:
                    found, observed = _json_pointer(artifact, pointer)
                    passed = found and observed == predicate.get("equals")
                    if not found:
                        error = "json_pointer_not_found"
        elif predicate_type == "oracle_state_equals":
            target_id = predicate.get("target_id")
            state = (
                oracle_states.get(str(target_id))
                if isinstance(oracle_states, dict)
                else None
            )
            if not isinstance(state, dict):
                error = "oracle_state_missing"
            else:
                observed = {
                    "exists": state.get("exists"),
                    "value": state.get("value"),
                }
                passed = (
                    state.get("exists") is predicate.get("exists")
                    and (
                        predicate.get("exists") is False
                        or state.get("value") == predicate.get("equals")
                    )
                )
        elif predicate_type == "causal_proof_predicate":
            proof_id = predicate.get("proof_receipt_id")
            observation = (
                causal_observations.get(str(proof_id))
                if isinstance(causal_observations, dict)
                else None
            )
            causal_predicate = predicate.get("predicate")
            evaluator = (
                _CAUSAL_PREDICATE_EVALUATORS.get(str(causal_predicate.get("kind")))
                if isinstance(causal_predicate, Mapping)
                else None
            )
            if not isinstance(observation, dict):
                error = "causal_observation_missing"
            elif observation.get("error") is not None:
                error = str(observation.get("error"))
                observed = observation.get("observed")
            elif evaluator is None or not isinstance(causal_predicate, Mapping):
                error = "causal_predicate_unavailable"
            else:
                observed = observation.get("observed")
                passed, error = evaluator(causal_predicate, observed)
        else:
            error = "predicate_type_invalid"
        row = {
            "predicate_index": index,
            "predicate": predicate,
            "observed": observed,
            "passed": passed,
            "error": error,
        }
        if artifact_receipt is not None:
            row["artifact_receipt"] = artifact_receipt
        results.append(row)
    return results


def run_outcome_evidence_role(
    *,
    workspace: Path,
    output_path: Path,
    role: str,
    role_contract: dict[str, Any],
    case_id: str,
    plan_revision_id: str,
    merged_commit: str,
    verification_contract_sha256: str,
    target_contract_sha256: str,
    verified_implementation_head: str,
    execution_commit: str | None = None,
    verification_amendment_id: str | None = None,
    timeout_seconds: float | None = None,
    recurrence_refresh_receipt_path: Path | None = None,
    recurrence_after: str | None = None,
    trusted_oracle_assets_root: Path | None = None,
) -> dict[str, Any]:
    """Execute one stage-6 proof role and persist a runner-owned machine result.

    ``timeout_seconds=None`` means no arbitrary wall-clock timeout. An explicit timeout
    never converts to success: it is retained as a blocked, failed role artifact.
    """

    workspace = workspace.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"outcome_role_workspace_missing:{workspace}")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("outcome_role_timeout_must_be_positive_or_none")
    normalized_contract = _normalize_role_contract(role, role_contract)
    case_id = _required_text(case_id, field="case_id")
    plan_revision_id = _required_text(plan_revision_id, field="plan_revision_id")
    verification_hash = _required_sha256(
        verification_contract_sha256,
        field="verification_contract_sha256",
    )
    target_hash = _required_sha256(
        target_contract_sha256,
        field="target_contract_sha256",
    )
    implementation_commit = _resolve_commit(
        workspace,
        _required_text(merged_commit, field="merged_commit"),
    )
    expected_commit = _resolve_commit(
        workspace,
        _required_text(
            execution_commit or implementation_commit,
            field="execution_commit",
        ),
    )
    if expected_commit == implementation_commit:
        if verification_amendment_id is not None:
            raise ValueError("outcome_role_verification_amendment_unexpected")
    elif (
        not isinstance(verification_amendment_id, str)
        or not verification_amendment_id.startswith("outcome_verification_amendment:")
    ):
        raise ValueError("outcome_role_verification_amendment_required")
    workspace_head = _resolved_head(workspace)
    if workspace_head != expected_commit:
        raise ValueError(
            "outcome_role_workspace_commit_mismatch:"
            f"expected={expected_commit}:observed={workspace_head}"
        )
    verified_head = _resolve_commit(
        workspace,
        _required_text(
            verified_implementation_head, field="verified_implementation_head"
        ),
    )
    _require_ancestor(
        workspace,
        ancestor=verified_head,
        descendant=implementation_commit,
        field="verified_implementation_head_to_merged_commit",
    )
    _require_ancestor(
        workspace,
        ancestor=implementation_commit,
        descendant=expected_commit,
        field="merged_commit_to_execution_commit",
    )

    recurrence_proof: dict[str, Any] | None = None
    if role == "recurrence":
        if recurrence_refresh_receipt_path is None or recurrence_after is None:
            raise ValueError("outcome_recurrence_fresh_shadow_receipt_required")
        recurrence_proof = _validate_recurrence_refresh(
            refresh_receipt_path=recurrence_refresh_receipt_path,
            case_id=case_id,
            plan_revision_id=plan_revision_id,
            recurrence_after=recurrence_after,
        )
    elif recurrence_refresh_receipt_path is not None or recurrence_after is not None:
        raise ValueError("outcome_recurrence_receipt_for_non_recurrence_role")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    oracle = normalized_contract.get("oracle")
    selected_contract_ids_raw = normalized_contract.get(
        "selected_positive_outcome_contract_ids"
    )
    selected_contract_ids = (
        {
            value
            for value in selected_contract_ids_raw
            if isinstance(value, str) and value
        }
        if isinstance(selected_contract_ids_raw, list)
        else None
    )
    positive_contract_source_receipts = (
        _verify_positive_contract_sources(
            oracle,
            workspace=workspace,
            selected_contract_ids=selected_contract_ids,
        )
        if isinstance(oracle, dict)
        else []
    )
    oracle_states: dict[str, dict[str, Any]] = {}
    causal_observations: dict[str, dict[str, Any]] = {}
    materialization: dict[str, Any] | None = None
    oracle_scenario_artifacts: list[dict[str, Any]] = []
    execution_integrity = True
    if isinstance(oracle, dict) and oracle.get("kind") == "multi_scenario":
        command_results = []
        for index, scenario in enumerate(oracle.get("scenarios", [])):
            child_oracle = scenario["oracle"]
            child_unsigned = {
                "description": (
                    "Post-change replay of one retained original scenario in the "
                    "canonical case bundle."
                ),
                "research_experiment_id": child_oracle.get("research_experiment_id"),
                "selected_positive_outcome_contract_ids": [
                    scenario["positive_outcome_contract_id"]
                ],
                "commands": [],
                "predicates": scenario["predicates"],
                "oracle": child_oracle,
                "required_proof_scope": child_oracle.get("proof_scope"),
                **(
                    {
                        "causal_proof_receipts": scenario.get(
                            "causal_proof_receipts"
                        )
                    }
                    if child_oracle.get("kind") == "causal_proof_replay"
                    else {}
                ),
            }
            child_contract = {
                **child_unsigned,
                "role_contract_sha256": _sha256_json(child_unsigned),
            }
            child_path = output_path.with_name(
                f"{output_path.stem}.scenario-{index:03d}{output_path.suffix}"
            )
            child_artifact = run_outcome_evidence_role(
                workspace=workspace,
                output_path=child_path,
                role=role,
                role_contract=child_contract,
                case_id=case_id,
                plan_revision_id=plan_revision_id,
                merged_commit=implementation_commit,
                verification_contract_sha256=verification_hash,
                target_contract_sha256=target_hash,
                verified_implementation_head=verified_head,
                execution_commit=expected_commit,
                verification_amendment_id=verification_amendment_id,
                timeout_seconds=timeout_seconds,
                trusted_oracle_assets_root=trusted_oracle_assets_root,
            )
            oracle_scenario_artifacts.append(
                {
                    "scenario_id": scenario["scenario_id"],
                    "positive_outcome_contract_id": scenario[
                        "positive_outcome_contract_id"
                    ],
                    "role_contract": child_contract,
                    "artifact": child_artifact,
                    "passed": child_artifact.get("passed"),
                    "timed_out": child_artifact.get("timed_out"),
                    "cancelled": child_artifact.get("cancelled"),
                    "execution_integrity": child_artifact.get("execution_integrity"),
                }
            )
        execution_integrity = all(
            value.get("execution_integrity") is True
            for value in oracle_scenario_artifacts
        )
    elif isinstance(oracle, dict) and oracle.get("kind") == "staged_replay":
        initial_status = _workspace_status(workspace)
        initial_manifest = _workspace_file_manifest(workspace)
        if initial_status:
            raise ValueError("outcome_oracle_workspace_not_clean")
        asset = oracle.get("asset")
        copied: list[Path] = []
        source_manifest: dict[str, Any] = {}
        try:
            if asset is not None:
                if trusted_oracle_assets_root is None or not isinstance(asset, dict):
                    raise ValueError("outcome_oracle_trusted_asset_root_required")
                source_root, source_manifest = _verify_oracle_asset(
                    asset,
                    trusted_root=trusted_oracle_assets_root,
                )
                copied = _materialize_oracle_asset(
                    source_root=source_root,
                    manifest=source_manifest,
                    workspace=workspace,
                )
            pre_execution_manifest = _workspace_file_manifest(workspace)
            pre_execution_status = _workspace_status(workspace)
            execution = oracle["execution"]
            command_results = [
                _run_argv(
                    list(execution["argv"]),
                    workspace=workspace,
                    timeout_seconds=timeout_seconds,
                )
            ]
            post_execution_manifest = _workspace_file_manifest(workspace)
            post_execution_status = _workspace_status(workspace)
            execution_integrity = (
                pre_execution_manifest == post_execution_manifest
                and pre_execution_status == post_execution_status
                and _resolved_head(workspace) == expected_commit
            )
        finally:
            _remove_materialized_asset(copied, workspace=workspace)
        final_manifest = _workspace_file_manifest(workspace)
        final_status = _workspace_status(workspace)
        cleanup_confirmed = (
            final_manifest == initial_manifest
            and final_status == initial_status == ""
            and _resolved_head(workspace) == expected_commit
        )
        execution_integrity = execution_integrity and cleanup_confirmed
        materialization = {
            "asset_id": asset.get("asset_id") if isinstance(asset, dict) else None,
            "source_manifest_sha256": (
                _sha256_json(source_manifest) if source_manifest else None
            ),
            "copied_paths": sorted(
                path.relative_to(workspace).as_posix() for path in copied
            ),
            "pre_execution_state_sha256": _sha256_json(pre_execution_manifest),
            "post_execution_state_sha256": _sha256_json(post_execution_manifest),
            "command_workspace_unchanged": (
                pre_execution_manifest == post_execution_manifest
                and pre_execution_status == post_execution_status
            ),
            "cleanup_confirmed": cleanup_confirmed,
            "final_status_clean": final_status == "",
            "final_head": _resolved_head(workspace),
        }
    elif isinstance(oracle, dict) and oracle.get("kind") == "causal_proof_replay":
        initial_status = _workspace_status(workspace)
        initial_manifest = _workspace_file_manifest(workspace)
        if initial_status:
            raise ValueError("outcome_oracle_workspace_not_clean")
        asset = oracle.get("asset")
        copied: list[Path] = []
        source_manifest: dict[str, Any] = {}
        disposable_paths: list[Path] = []
        normalized_disposable_paths: list[str] = []
        pre_execution_manifest: dict[str, dict[str, Any]] = {}
        post_execution_manifest: dict[str, dict[str, Any]] = {}
        try:
            if asset is not None:
                if trusted_oracle_assets_root is None or not isinstance(asset, dict):
                    raise ValueError("outcome_oracle_trusted_asset_root_required")
                source_root, source_manifest = _verify_oracle_asset(
                    asset,
                    trusted_root=trusted_oracle_assets_root,
                )
                copied = _materialize_oracle_asset(
                    source_root=source_root,
                    manifest=source_manifest,
                    workspace=workspace,
                )
            execution = oracle["execution"]
            causal_proofs = {
                str(proof["proof_receipt_id"]): proof
                for proof in normalized_contract.get("causal_proof_receipts", [])
                if isinstance(proof, Mapping)
            }
            environment_overrides, disposable_paths, normalized_disposable_paths = (
                _causal_replay_setup(
                    execution,
                    workspace=workspace,
                    causal_proofs=causal_proofs,
                )
            )
            pre_execution_manifest = _workspace_file_manifest(workspace)
            command_results = [
                _run_argv(
                    list(execution["argv"]),
                    workspace=workspace,
                    timeout_seconds=timeout_seconds,
                    environment_overrides=environment_overrides,
                )
            ]
            causal_observations = _causal_observation_results(
                causal_proofs,
                commands=command_results,
                workspace=workspace,
            )
            post_execution_manifest = _workspace_file_manifest(workspace)
            changed_paths = {
                path
                for path in set(pre_execution_manifest) | set(post_execution_manifest)
                if pre_execution_manifest.get(path) != post_execution_manifest.get(path)
            }
            disposable_roots = [Path(path) for path in normalized_disposable_paths]
            changes_confined = all(
                any(
                    Path(path) == root or root in Path(path).parents
                    for root in disposable_roots
                )
                for path in changed_paths
            )
            execution_integrity = changes_confined and _resolved_head(workspace) == expected_commit
        finally:
            _remove_causal_disposable_state(disposable_paths, workspace=workspace)
            _remove_materialized_asset(copied, workspace=workspace)
        final_manifest = _workspace_file_manifest(workspace)
        final_status = _workspace_status(workspace)
        cleanup_confirmed = (
            final_manifest == initial_manifest
            and final_status == initial_status == ""
            and _resolved_head(workspace) == expected_commit
        )
        execution_integrity = execution_integrity and cleanup_confirmed
        materialization = {
            "asset_id": asset.get("asset_id") if isinstance(asset, dict) else None,
            "source_manifest_sha256": (
                _sha256_json(source_manifest) if source_manifest else None
            ),
            "copied_paths": sorted(
                path.relative_to(workspace).as_posix() for path in copied
            ),
            "disposable_state_paths": normalized_disposable_paths,
            "pre_execution_state_sha256": _sha256_json(pre_execution_manifest),
            "post_execution_state_sha256": _sha256_json(post_execution_manifest),
            "cleanup_confirmed": cleanup_confirmed,
            "final_status_clean": final_status == "",
            "final_head": _resolved_head(workspace),
        }
    elif isinstance(oracle, dict) and oracle.get("kind") == "config_state":
        initial_status = _workspace_status(workspace)
        if initial_status:
            raise ValueError("outcome_oracle_workspace_not_clean")
        command_results = []
        for target in oracle.get("state_targets", []):
            if not isinstance(target, dict):
                raise ValueError("outcome_oracle_config_target_invalid")
            state = _read_config_state(workspace, target)
            oracle_states[str(state["target_id"])] = state
        execution_integrity = (
            _workspace_status(workspace) == "" and _resolved_head(workspace) == expected_commit
        )
    else:
        command_results = [
            _run_command(
                command,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
            )
            for command in normalized_contract["commands"]
        ]
    predicate_results = _evaluate_predicates(
        normalized_contract["predicates"],
        commands=command_results,
        workspace=workspace,
        output_dir=output_path.parent,
        oracle_states=oracle_states,
        oracle_scenarios=oracle_scenario_artifacts,
        causal_observations=causal_observations,
    )
    timed_out = any(result["timed_out"] is True for result in command_results) or any(
        result.get("timed_out") is True for result in oracle_scenario_artifacts
    )
    cancelled = any(result["cancelled"] is True for result in command_results) or any(
        result.get("cancelled") is True for result in oracle_scenario_artifacts
    )
    passed = bool(
        (predicate_results or role == "recurrence")
        and all(result["passed"] is True for result in predicate_results)
        and not timed_out
        and not cancelled
        and execution_integrity
    )
    artifact = {
        "schema_version": 1,
        "producer": "runner_core",
        "role": role,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "merged_commit": implementation_commit,
        "workspace": str(workspace),
        "workspace_head": workspace_head,
        "verification_contract_sha256": verification_hash,
        "target_contract_sha256": target_hash,
        "verified_implementation_head": verified_head,
        "role_contract_sha256": normalized_contract["role_contract_sha256"],
        "role_contract": normalized_contract,
        "outcome_oracle_id": (
            oracle.get("outcome_oracle_id") if isinstance(oracle, dict) else None
        ),
        "proof_scope": oracle.get("proof_scope") if isinstance(oracle, dict) else None,
        "positive_contract_source_receipts": positive_contract_source_receipts,
        "timeout_seconds": timeout_seconds,
        "commands": command_results,
        "predicate_results": predicate_results,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "execution_integrity": execution_integrity,
        "passed": passed,
    }
    if expected_commit != implementation_commit:
        artifact["execution_commit"] = expected_commit
        artifact["verification_amendment_id"] = verification_amendment_id
    if materialization is not None:
        artifact["oracle_materialization"] = materialization
    if oracle_states:
        artifact["oracle_states"] = [oracle_states[key] for key in sorted(oracle_states)]
    if oracle_scenario_artifacts:
        artifact["oracle_scenario_artifacts"] = oracle_scenario_artifacts
    if causal_observations:
        artifact["causal_observations"] = [
            causal_observations[key] for key in sorted(causal_observations)
        ]
    if recurrence_proof is not None:
        artifact["recurrence_refresh_proof"] = recurrence_proof
    artifact["artifact_content_sha256"] = _sha256_json(artifact)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(output_path)
    return artifact


def validate_outcome_evidence_role_artifact(
    artifact: Any,
    *,
    role: str,
    case_id: str,
    plan_revision_id: str,
    merged_commit: str,
    verification_contract_sha256: str,
    target_contract_sha256: str,
    verified_implementation_head: str,
    role_contract: dict[str, Any],
    execution_commit: str | None = None,
    verification_amendment_id: str | None = None,
) -> dict[str, Any]:
    """Revalidate a retained runner artifact without trusting receipt claims."""

    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1:
        raise ValueError("outcome_role_artifact_schema_invalid")
    content_hash = artifact.get("artifact_content_sha256")
    unsigned = {key: value for key, value in artifact.items() if key != "artifact_content_sha256"}
    if content_hash != _sha256_json(unsigned):
        raise ValueError("outcome_role_artifact_content_hash_mismatch")
    normalized_contract = _normalize_role_contract(role, role_contract)
    expected = {
        "producer": "runner_core",
        "role": role,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "merged_commit": merged_commit.casefold(),
        "verification_contract_sha256": verification_contract_sha256.casefold(),
        "target_contract_sha256": target_contract_sha256.casefold(),
        "verified_implementation_head": verified_implementation_head.casefold(),
        "role_contract_sha256": normalized_contract["role_contract_sha256"],
        "role_contract": normalized_contract,
    }
    normalized_implementation = merged_commit.casefold()
    normalized_execution = (execution_commit or merged_commit).casefold()
    if normalized_execution != normalized_implementation:
        if (
            not isinstance(verification_amendment_id, str)
            or not verification_amendment_id.startswith(
                "outcome_verification_amendment:"
            )
        ):
            raise ValueError("outcome_role_artifact_verification_amendment_required")
        expected["execution_commit"] = normalized_execution
        expected["verification_amendment_id"] = verification_amendment_id
    elif verification_amendment_id is not None:
        raise ValueError("outcome_role_artifact_verification_amendment_unexpected")
    for field, value in expected.items():
        observed = artifact.get(field)
        if isinstance(value, str) and field.endswith("sha256"):
            observed = str(observed or "").casefold()
        if observed != value:
            raise ValueError(f"outcome_role_artifact_identity_mismatch:{field}")
    if role == "recurrence":
        stored_proof = artifact.get("recurrence_refresh_proof")
        if not isinstance(stored_proof, dict):
            raise ValueError("outcome_recurrence_refresh_proof_missing")
        refreshed_proof = _validate_recurrence_refresh(
            refresh_receipt_path=Path(
                _required_text(
                    stored_proof.get("refresh_receipt_path"),
                    field="refresh_receipt_path",
                )
            ),
            case_id=case_id,
            plan_revision_id=plan_revision_id,
            recurrence_after=_required_text(
                stored_proof.get("recurrence_after"), field="recurrence_after"
            ),
        )
        if refreshed_proof != stored_proof:
            raise ValueError("outcome_recurrence_refresh_proof_changed")
    elif "recurrence_refresh_proof" in artifact:
        raise ValueError("outcome_recurrence_refresh_proof_unexpected")
    commands = artifact.get("commands")
    predicates = artifact.get("predicate_results")
    oracle = normalized_contract.get("oracle")
    oracle_kind = oracle.get("kind") if isinstance(oracle, dict) else None
    expected_command_count = (
        1
        if oracle_kind in {"staged_replay", "causal_proof_replay"}
        else 0
        if oracle_kind == "multi_scenario"
        else len(normalized_contract["commands"])
    )
    if not isinstance(commands, list) or len(commands) != expected_command_count:
        raise ValueError("outcome_role_artifact_command_coverage_invalid")
    expected_commands = normalized_contract["commands"]
    if oracle_kind in {"staged_replay", "causal_proof_replay"}:
        expected_commands = [" ".join(oracle["execution"]["argv"])]
    for index, (expected_command, result) in enumerate(
        zip(expected_commands, commands, strict=True)
    ):
        if not isinstance(result, dict) or result.get("command") != expected_command:
            raise ValueError(f"outcome_role_artifact_command_identity_mismatch:{index}")
        if (
            oracle_kind in {"staged_replay", "causal_proof_replay"}
            and result.get("argv") != oracle["execution"]["argv"]
        ):
            raise ValueError(f"outcome_role_artifact_argv_identity_mismatch:{index}")
        if result.get("timed_out") is not False or result.get("cancelled") is not False:
            raise ValueError(f"outcome_role_artifact_command_blocked:{index}")
    if not isinstance(predicates, list) or len(predicates) != len(
        normalized_contract["predicates"]
    ):
        raise ValueError("outcome_role_artifact_predicate_coverage_invalid")
    for index, (expected_predicate, result) in enumerate(
        zip(normalized_contract["predicates"], predicates, strict=True)
    ):
        if (
            not isinstance(result, dict)
            or result.get("predicate_index") != index
            or result.get("predicate") != expected_predicate
            or result.get("passed") is not True
            or result.get("error") is not None
        ):
            raise ValueError(f"outcome_role_artifact_predicate_not_passed:{index}")
        receipt = result.get("artifact_receipt")
        if isinstance(receipt, dict):
            snapshot_raw = receipt.get("snapshot_path")
            if not isinstance(snapshot_raw, str):
                raise ValueError(f"outcome_role_artifact_snapshot_path_invalid:{index}")
            snapshot = Path(snapshot_raw).expanduser().resolve()
            if not snapshot.is_file() or _sha256_file(snapshot) != receipt.get(
                "snapshot_sha256"
            ):
                raise ValueError(f"outcome_role_artifact_snapshot_changed:{index}")
    if (
        artifact.get("passed") is not True
        or artifact.get("timed_out") is not False
        or artifact.get("cancelled") is not False
        or artifact.get("execution_integrity") is not True
    ):
        raise ValueError("outcome_role_artifact_not_terminal_pass")
    if isinstance(oracle, dict):
        retained_workspace = Path(
            _required_text(artifact.get("workspace"), field="workspace")
        ).expanduser().resolve()
        expected_positive_sources = _verify_positive_contract_sources(
            oracle,
            workspace=retained_workspace,
            selected_contract_ids=(
                {
                    value
                    for value in normalized_contract.get(
                        "selected_positive_outcome_contract_ids", []
                    )
                    if isinstance(value, str) and value
                }
                or None
            ),
        )
        if artifact.get("positive_contract_source_receipts") != expected_positive_sources:
            raise ValueError("outcome_role_positive_contract_sources_changed")
        if (
            artifact.get("outcome_oracle_id") != oracle.get("outcome_oracle_id")
            or artifact.get("proof_scope") != oracle.get("proof_scope")
        ):
            raise ValueError("outcome_role_artifact_oracle_identity_mismatch")
        if oracle_kind == "staged_replay":
            materialization = artifact.get("oracle_materialization")
            if (
                not isinstance(materialization, dict)
                or materialization.get("cleanup_confirmed") is not True
                or materialization.get("final_status_clean") is not True
                or materialization.get("command_workspace_unchanged") is not True
            ):
                raise ValueError("outcome_role_artifact_materialization_invalid")
        elif oracle_kind == "causal_proof_replay":
            materialization = artifact.get("oracle_materialization")
            observations = artifact.get("causal_observations")
            expected_proof_ids = {
                str(proof.get("proof_receipt_id"))
                for proof in normalized_contract.get("causal_proof_receipts", [])
                if isinstance(proof, Mapping)
            }
            if (
                not isinstance(materialization, dict)
                or materialization.get("cleanup_confirmed") is not True
                or materialization.get("final_status_clean") is not True
                or not isinstance(observations, list)
                or {
                    str(observation.get("proof_receipt_id"))
                    for observation in observations
                    if isinstance(observation, Mapping)
                }
                != expected_proof_ids
                or any(
                    not isinstance(observation, Mapping)
                    or observation.get("error") is not None
                    or observation.get("observed_sha256")
                    != _sha256_json(observation.get("observed"))
                    for observation in observations
                )
            ):
                raise ValueError("outcome_role_artifact_causal_replay_invalid")
        elif oracle_kind == "config_state":
            states = artifact.get("oracle_states")
            if not isinstance(states, list) or {
                state.get("target_id") for state in states if isinstance(state, dict)
            } != {
                target.get("target_id")
                for target in oracle.get("state_targets", [])
                if isinstance(target, dict)
            }:
                raise ValueError("outcome_role_artifact_config_state_invalid")
        elif oracle_kind == "multi_scenario":
            stored_scenarios = artifact.get("oracle_scenario_artifacts")
            declared_scenarios = oracle.get("scenarios")
            if (
                not isinstance(stored_scenarios, list)
                or not isinstance(declared_scenarios, list)
                or len(stored_scenarios) != len(declared_scenarios)
            ):
                raise ValueError("outcome_role_artifact_multi_scenario_coverage_invalid")
            for index, (stored, declared) in enumerate(
                zip(stored_scenarios, declared_scenarios, strict=True)
            ):
                if (
                    not isinstance(stored, dict)
                    or not isinstance(declared, dict)
                    or stored.get("scenario_id") != declared.get("scenario_id")
                    or stored.get("positive_outcome_contract_id")
                    != declared.get("positive_outcome_contract_id")
                    or stored.get("role_contract") is None
                    or stored.get("artifact") is None
                ):
                    raise ValueError(
                        f"outcome_role_artifact_multi_scenario_identity_invalid:{index}"
                    )
                validate_outcome_evidence_role_artifact(
                    stored["artifact"],
                    role=role,
                    case_id=case_id,
                    plan_revision_id=plan_revision_id,
                    merged_commit=merged_commit,
                    verification_contract_sha256=verification_contract_sha256,
                    target_contract_sha256=target_contract_sha256,
                    verified_implementation_head=verified_implementation_head,
                    role_contract=stored["role_contract"],
                    execution_commit=execution_commit,
                    verification_amendment_id=verification_amendment_id,
                )
    return dict(artifact)


__all__ = [
    "OUTCOME_EVIDENCE_ROLES",
    "register_causal_observation_source",
    "register_causal_outcome_predicate",
    "run_outcome_evidence_role",
    "validate_outcome_evidence_role_artifact",
]
