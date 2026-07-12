# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_WINDOWS_OFFLINE_FIRST_SUCCESS_CMD = (
    r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1"
)
_POSIX_OFFLINE_FIRST_SUCCESS_CMD = "bash ./scripts/offline_first_success.sh"
_BREADTH_PROFILE_EXTERNAL = "external_generalization"
_BREADTH_PROFILE_INTERNAL = "internal_maintenance"
_BREADTH_PROFILE_CHOICES = (
    _BREADTH_PROFILE_EXTERNAL,
    _BREADTH_PROFILE_INTERNAL,
)
_BREADTH_DIMENSIONS = ("runs", "missions", "targets", "repo_inputs", "agents", "personas")
_BREADTH_CONTEXT_DIMENSIONS = ("missions", "targets", "repo_inputs")
_BREADTH_OBSERVATION_DIMENSIONS = ("runs", "agents", "personas")
_REVIEW_DOMAIN_COMMAND_SURFACE = "command_surface"
_REVIEW_DOMAIN_BEHAVIOR_COMPAT = "behavior_compat"
_SOURCE_RELATIVE_PATHS = (
    "apps/usertest/src",
    "apps/usertest_backlog/src",
    "apps/usertest_implement/src",
    "packages/runner_core/src",
    "packages/agent_adapters/src",
    "packages/normalized_events/src",
    "packages/reporter/src",
    "packages/sandbox_runner/src",
    "packages/triage_engine/src",
    "packages/backlog_core/src",
    "packages/backlog_miner/src",
    "packages/backlog_repo/src",
    "packages/token_monitoring/src",
    "packages/run_artifacts/src",
)


def _prefer_checkout_sources() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    if not (repo_root / "tools" / "scaffold" / "monorepo.toml").exists():
        return
    for rel_path in reversed(_SOURCE_RELATIVE_PATHS):
        source_path = (repo_root / rel_path).resolve()
        if not source_path.exists():
            continue
        source_path_s = str(source_path)
        if source_path_s in sys.path:
            sys.path.remove(source_path_s)
        sys.path.insert(0, source_path_s)


_prefer_checkout_sources()


def _one_command_first_success_remediation() -> str:
    return (
        "Quick fix (recommended): from repo root, run ONE of:\n"
        f"  - Windows PowerShell: `{_WINDOWS_OFFLINE_FIRST_SUCCESS_CMD}`\n"
        f"  - macOS/Linux: `{_POSIX_OFFLINE_FIRST_SUCCESS_CMD}`"
    )


def _missing_dependency_remediation(*, dependency: str, import_name: str) -> str:
    return (
        f"Missing dependency `{dependency}` (import name: `{import_name}`).\n"
        f"{_one_command_first_success_remediation()}\n"
        "Manual fix (preferred): from `apps/usertest_backlog`, run `pdm install -d`."
    )


def _missing_dependency_remediation_simple(*, dependency: str) -> str:
    return (
        f"Missing dependency `{dependency}`.\n"
        f"{_one_command_first_success_remediation()}\n"
        "Manual fix (preferred): from `apps/usertest_backlog`, run `pdm install -d`."
    )


try:
    import yaml
except ModuleNotFoundError as exc:
    raise SystemExit(
        _missing_dependency_remediation(dependency="pyyaml", import_name="yaml")
    ) from exc
try:
    import jsonschema  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(_missing_dependency_remediation_simple(dependency="jsonschema")) from exc


def _from_source_import_remediation(*, missing_module: str) -> str:
    return (
        f"Missing import `{missing_module}`.\n"
        f"{_one_command_first_success_remediation()}\n"
        "Manual fix (preferred): from `apps/usertest_backlog`, run `pdm install -d` and then\n"
        "run the CLI via `pdm run usertest-backlog ...`."
    )


def _is_missing_module(exc: ModuleNotFoundError, module: str) -> bool:
    name = getattr(exc, "name", None)
    if not name:
        return False
    return name == module or name.startswith(f"{module}.")


try:
    from backlog_core import (
        add_atom_links,
        assemble_backlog_tickets,
        build_backlog_document,
        extract_backlog_atoms,
        write_backlog,
        write_backlog_atoms,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "backlog_core"):
        raise SystemExit(_from_source_import_remediation(missing_module="backlog_core")) from exc
    raise

try:
    from backlog_core.aggregate_metrics import build_aggregate_metrics_atoms
    from backlog_core.backlog_policy import BacklogPolicyConfig, apply_backlog_policy
    from backlog_core.case_lineage import (
        ATOM_DISPOSITIONS,
        TERMINAL_CASE_STATES,
        apply_atom_disposition_decision,
        apply_atom_dispositions,
        assign_problem_case_ids,
        atom_disposition_summary,
        atom_is_idea_originated,
        attach_supporting_atoms_to_problem_cases,
        build_case_registry,
        eligible_problem_mining_atoms,
        load_case_registry,
        normalize_atom_lineage,
        problem_case_records_from_registry,
        propagate_case_lineage,
        update_case_registry_stage_lineage,
        write_case_registry,
    )
    from backlog_core.prioritization import compute_problem_priority_signals
    from backlog_core.relation_review import (
        apply_relation_decisions,
        canonicalize_problem_cases,
        rank_stage_related_items,
    )
    from backlog_core.stage_contracts import (
        build_stage_document,
        parse_change_plan_list,
        parse_priority_decision_list,
        parse_problem_record_list,
        parse_selection_decisions,
        parse_solution_option_sets,
        research_prompt_projection,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "backlog_core"):
        raise SystemExit(_from_source_import_remediation(missing_module="backlog_core")) from exc
    raise

try:
    from backlog_miner import (
        run_backlog_prompt,
        run_repro_research_stage,
    )
    from backlog_miner.pipeline import (
        ModelInvocationTracker,
        PipelinePromptManifest,
        attach_stage_model_invocation_contract,
        load_pipeline_prompt_manifest,
        merge_stage_model_invocation_contract,
        run_stage_prompt_json,
        run_stage_prompt_json_result,
        verify_stage_model_invocation_contract,
    )
    from backlog_miner.prompt_correction import (
        CorrectionObservation,
        correction_run_metrics,
        correction_state_sha256,
        run_progressive_correction,
    )
    from backlog_miner.research_evidence import verify_persisted_research_evidence
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "backlog_miner"):
        raise SystemExit(_from_source_import_remediation(missing_module="backlog_miner")) from exc
    raise

try:
    from backlog_repo import (
        archive_plan_ticket_file as _archive_plan_ticket_file,
    )
    from backlog_repo import (
        canonicalize_failure_atom_id as _canonicalize_failure_atom_id,
    )
    from backlog_repo import (
        dedupe_actioned_plan_ticket_files as _dedupe_actioned_plan_ticket_files,
    )
    from backlog_repo import (
        load_atom_actions_yaml as _load_atom_actions_yaml,
    )
    from backlog_repo import (
        load_backlog_actions_yaml as _load_backlog_actions_yaml,
    )
    from backlog_repo import (
        normalize_atom_status as _normalize_atom_status,
    )
    from backlog_repo import (
        outcome_suppresses_new_case_discovery as _outcome_suppresses_new_case_discovery,
    )
    from backlog_repo import (
        promote_atom_status as _promote_atom_status,
    )
    from backlog_repo import (
        reconcile_atom_actions_from_plan_folders as _reconcile_atom_actions_from_plan_folders,
    )
    from backlog_repo import (
        scan_plan_ticket_index as _scan_plan_ticket_index,
    )
    from backlog_repo import (
        sorted_unique_strings as _sorted_unique_strings,
    )
    from backlog_repo import validate_outcome_record as _validate_outcome_record
    from backlog_repo import (
        write_atom_actions_yaml as _write_atom_actions_yaml,
    )
    from backlog_repo import (
        write_backlog_actions_yaml as _write_backlog_actions_yaml,
    )
    from backlog_repo.export import (
        ticket_export_case_id,
        ticket_export_fingerprint,
        ticket_export_plan_revision_id,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "backlog_repo"):
        raise SystemExit(_from_source_import_remediation(missing_module="backlog_repo")) from exc
    raise

try:
    from reporter import (
        analyze_report_history,
        build_window_summary,
        write_issue_analysis,
        write_window_summary,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "reporter"):
        raise SystemExit(_from_source_import_remediation(missing_module="reporter")) from exc
    raise

try:
    from run_artifacts.history import (
        iter_report_history,
        load_run_record,
        select_recent_run_dirs,
        write_report_history_jsonl,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "run_artifacts"):
        raise SystemExit(_from_source_import_remediation(missing_module="run_artifacts")) from exc
    raise

try:
    from runner_core import RunnerConfig, find_repo_root
    from runner_core.pathing import slugify
    from runner_core.target_acquire import acquire_target
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "runner_core"):
        raise SystemExit(_from_source_import_remediation(missing_module="runner_core")) from exc
    raise

try:
    from triage_engine import cluster_items, extract_path_anchors_from_chunks
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "triage_engine"):
        raise SystemExit(_from_source_import_remediation(missing_module="triage_engine")) from exc
    raise

try:
    from usertest_backlog.triage_atoms import (
        build_implementation_index,
        build_plan_status_index,
        infer_backlog_json,
        load_atoms_jsonl,
        load_backlog_json,
        resolve_embedder,
        write_triage_atoms,
    )
    from usertest_backlog.triage_atoms import (
        triage_atoms as triage_atoms_report,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"usertest_backlog", "usertest_backlog.triage_atoms"}:
        raise SystemExit(
            _from_source_import_remediation(missing_module="usertest_backlog")
        ) from exc
    raise

try:
    from usertest_backlog.triage_backlog import (
        load_issue_items,
        triage_issues,
        write_triage_xlsx,
    )
    from usertest_backlog.triage_backlog import (
        render_triage_markdown as render_backlog_triage_markdown,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"usertest_backlog", "usertest_backlog.triage_backlog"}:
        raise SystemExit(
            _from_source_import_remediation(missing_module="usertest_backlog")
        ) from exc
    raise

_EXPORT_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "blocker": 3}
_MONOREPO_OWNER_COMPONENTS: set[str] = {"runner_core", "agent_adapters", "sandbox_runner"}
_ATOM_STATUS_ORDER: dict[str, int] = {"new": 0, "ticketed": 1, "queued": 2, "actioned": 3}
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
try:
    from runner_core.python_interpreter_probe import probe_python_interpreters
except ModuleNotFoundError:
    probe_python_interpreters = None  # type: ignore[assignment]


def _enable_console_backslashreplace(stream: Any) -> None:
    """Handle enable console backslashreplace processing.

    Parameters
    ----------
    stream:
        Console stream to configure.

    Returns
    -------
    None
        None.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        if str(getattr(stream, "errors", "")).lower() == "backslashreplace":
            return
        reconfigure(errors="backslashreplace")
    except (OSError, ValueError):
        return


def _configure_console_output() -> None:
    """Handle configure console output processing.

    Returns
    -------
    None
        None.
    """
    _enable_console_backslashreplace(sys.stdout)
    _enable_console_backslashreplace(sys.stderr)


_configure_console_output()


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load yaml from disk or config inputs.

    Parameters
    ----------
    path:
        Filesystem path input.

    Returns
    -------
    dict[str, Any]
        Structured mapping result.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(data).__name__}")
    return data


def _load_runner_config(repo_root: Path) -> RunnerConfig:
    """Load runner config from disk or config inputs.

    Parameters
    ----------
    repo_root:
        Repository root path.

    Returns
    -------
    RunnerConfig
        Computed return value.
    """
    agents_cfg = _load_yaml(repo_root / "configs" / "agents.yaml").get("agents", {})
    policies_cfg = _load_yaml(repo_root / "configs" / "policies.yaml").get("policies", {})
    if not isinstance(agents_cfg, dict) or not isinstance(policies_cfg, dict):
        raise ValueError("Invalid configs under configs/.")
    return RunnerConfig(
        repo_root=repo_root,
        runs_dir=repo_root / "runs" / "usertest",
        agents=agents_cfg,
        policies=policies_cfg,
    )


def _looks_like_local_repo_input(repo: str) -> bool:
    """Return whether input looks like local repo input.

    Parameters
    ----------
    repo:
        Repository input string.

    Returns
    -------
    bool
        Boolean decision result.
    """
    raw = repo.strip()
    if not raw:
        return False
    if raw.startswith(("http://", "https://", "git@")):
        return False
    if raw.startswith(("pip:", "pdm:")):
        return False
    if _WINDOWS_ABS_PATH_RE.match(raw):
        return True
    if raw.startswith(("\\\\", "/", "./", "../", ".\\", "..\\", "~")):
        return True
    return ("\\" in raw) or ("/" in raw)


def _resolve_local_repo_root(repo_root: Path, repo: str) -> Path | None:
    """Resolve local repo root from provided inputs.

    Parameters
    ----------
    repo_root:
        Repository root path.
    repo:
        Repository input string.

    Returns
    -------
    Path | None
        Resolved filesystem path value.
    """
    try:
        candidate = Path(repo).expanduser()
    except OSError:
        return None
    if candidate.exists():
        try:
            return candidate.resolve()
        except OSError:
            return candidate
    if not candidate.is_absolute():
        alt = (repo_root / candidate).expanduser()
        if alt.exists():
            try:
                return alt.resolve()
            except OSError:
                return alt
    return None


def _infer_responsiveness_probe_commands(repo_dir: Path) -> set[str]:
    """Infer responsiveness probe commands from available context.

    Parameters
    ----------
    repo_dir:
        Repository directory path.

    Returns
    -------
    set[str]
        Computed return value.
    """
    commands: set[str] = set()
    if (repo_dir / "package.json").exists():
        commands.update({"node", "npm"})
    return commands


def _probe_command_responsive(*, command: str, timeout_seconds: float) -> str | None:
    """Probe command responsive availability.

    Parameters
    ----------
    command:
        Input parameter.
    timeout_seconds:
        Input parameter.

    Returns
    -------
    str | None
        Computed return value.
    """
    if command in {"python", "python3", "py"} and callable(probe_python_interpreters):
        probe = probe_python_interpreters(
            candidate_commands=[command],
            timeout_seconds=max(0.1, timeout_seconds),
        )
        candidate = probe.by_command().get(command)
        if candidate is None or not candidate.present:
            return None
        if candidate.usable:
            return None
        code = candidate.reason_code or "probe_failed"
        reason = candidate.reason or "interpreter health probe failed"
        return f"command {command!r} resolves to an unusable Python interpreter ({code}): {reason}"

    resolved = shutil.which(command)
    if resolved is None:
        return None
    try:
        subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            f"command {command!r} appears unresponsive (timed out after {timeout_seconds:.1f}s "
            f"running `{command} --version`)."
        )
    except OSError as e:
        return f"command {command!r} probe failed: {e}"
    return None


def _resolve_repo_root(arg: Path | None) -> Path:
    """Resolve repo root from provided inputs.

    Parameters
    ----------
    arg:
        Input parameter.

    Returns
    -------
    Path
        Resolved filesystem path value.
    """
    if arg is not None:
        return arg.resolve()
    return find_repo_root()


def _resolve_optional_path(repo_root: Path, arg: Path | None) -> Path | None:
    """Resolve optional path from provided inputs.

    Parameters
    ----------
    repo_root:
        Repository root path.
    arg:
        Input parameter.

    Returns
    -------
    Path | None
        Resolved filesystem path value.
    """
    if arg is None:
        return None
    path = arg
    if not path.is_absolute() and not path.exists():
        path = repo_root / path
    return path.resolve()


def _normalize_breadth_profile(value: Any) -> str:
    profile = _coerce_string(value) or _BREADTH_PROFILE_EXTERNAL
    if profile not in _BREADTH_PROFILE_CHOICES:
        return _BREADTH_PROFILE_EXTERNAL
    return profile


def _default_breadth_profile_prompts_dir(repo_root: Path, breadth_profile: str) -> Path:
    profile = _normalize_breadth_profile(breadth_profile)
    if profile == _BREADTH_PROFILE_INTERNAL:
        return repo_root / "configs" / "backlog_prompts_internal_maintenance"
    return repo_root / "configs" / "backlog_prompts"


def _default_breadth_profile_policy_path(repo_root: Path, breadth_profile: str) -> Path:
    profile = _normalize_breadth_profile(breadth_profile)
    if profile == _BREADTH_PROFILE_INTERNAL:
        return repo_root / "configs" / "backlog_policy_internal_maintenance.yaml"
    return repo_root / "configs" / "backlog_policy.yaml"


def _resolve_breadth_profile_paths(
    *,
    repo_root: Path,
    breadth_profile: str,
    prompts_dir_arg: Path | None,
    policy_config_arg: Path | None,
) -> tuple[Path, Path, list[str]]:
    prompts_dir = (
        _resolve_optional_path(repo_root, prompts_dir_arg)
        if prompts_dir_arg is not None
        else _default_breadth_profile_prompts_dir(repo_root, breadth_profile)
    )
    policy_path = (
        _resolve_optional_path(repo_root, policy_config_arg)
        if policy_config_arg is not None
        else _default_breadth_profile_policy_path(repo_root, breadth_profile)
    )

    warnings_list: list[str] = []
    if prompts_dir_arg is not None:
        warnings_list.append(
            "breadth-profile prompts-dir override active: "
            f"profile={breadth_profile} prompts_dir={prompts_dir}"
        )
    if policy_config_arg is not None:
        warnings_list.append(
            "breadth-profile policy-config override active: "
            f"profile={breadth_profile} policy_config={policy_path}"
        )
    return prompts_dir, policy_path, warnings_list


def _coerce_string(value: Any) -> str | None:
    """Coerce input into string form.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    str | None
        Computed return value.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_string_list(value: Any) -> list[str]:
    """Coerce input into string list form.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    list[str]
        Normalized list result.
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _coerce_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _coerce_breadth_counts(value: Any) -> dict[str, int]:
    mapping = value if isinstance(value, dict) else {}
    return {dim: int(_coerce_int(mapping.get(dim), default=0)) for dim in _BREADTH_DIMENSIONS}


def compute_problem_breadth(
    evidence_atom_ids: list[str],
    atoms_by_id: dict[str, dict[str, Any]],
) -> dict[str, int]:
    unique_values: dict[str, set[str]] = {dim: set() for dim in _BREADTH_DIMENSIONS}
    field_map = {
        "runs": "run_rel",
        "missions": "mission_id",
        "targets": "target_slug",
        "repo_inputs": "repo_input",
        "agents": "agent",
        "personas": "persona_id",
    }
    for atom_id in evidence_atom_ids:
        atom = atoms_by_id.get(atom_id)
        if atom is None:
            continue
        for dim, field_name in field_map.items():
            value = _coerce_string(atom.get(field_name))
            if value is not None:
                unique_values[dim].add(value)
    return {dim: len(unique_values[dim]) for dim in _BREADTH_DIMENSIONS}


def compute_batch_breadth(atoms: list[dict[str, Any]]) -> dict[str, Any]:
    unique_values: dict[str, set[str]] = {dim: set() for dim in _BREADTH_DIMENSIONS}
    field_map = {
        "runs": "run_rel",
        "missions": "mission_id",
        "targets": "target_slug",
        "repo_inputs": "repo_input",
        "agents": "agent",
        "personas": "persona_id",
    }
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        for dim, field_name in field_map.items():
            value = _coerce_string(atom.get(field_name))
            if value is not None:
                unique_values[dim].add(value)

    counts = {dim: len(unique_values[dim]) for dim in _BREADTH_DIMENSIONS}
    struct_const = [dim for dim in _BREADTH_DIMENSIONS if counts.get(dim, 0) == 1]
    varying = [dim for dim in _BREADTH_DIMENSIONS if counts.get(dim, 0) > 1]
    return {
        **counts,
        "structurally_constant_dimensions": struct_const,
        "varying_dimensions": varying,
    }


def _coerce_batch_breadth(value: Any) -> dict[str, Any]:
    counts = _coerce_breadth_counts(value)
    raw = value if isinstance(value, dict) else {}
    struct_const = [
        dim
        for dim in raw.get("structurally_constant_dimensions", [])
        if isinstance(dim, str) and dim in _BREADTH_DIMENSIONS
    ]
    varying = [
        dim
        for dim in raw.get("varying_dimensions", [])
        if isinstance(dim, str) and dim in _BREADTH_DIMENSIONS
    ]
    if not struct_const:
        struct_const = [dim for dim in _BREADTH_DIMENSIONS if counts.get(dim, 0) == 1]
    if not varying:
        varying = [dim for dim in _BREADTH_DIMENSIONS if counts.get(dim, 0) > 1]
    return {
        **counts,
        "structurally_constant_dimensions": struct_const,
        "varying_dimensions": varying,
    }


def _build_decision_basis(
    *,
    problem_breadth: dict[str, int],
    batch_breadth: dict[str, Any] | None,
) -> dict[str, Any]:
    batch = batch_breadth if isinstance(batch_breadth, dict) else {}
    return {
        "context_breadth": {
            dim: int(problem_breadth.get(dim, 0)) for dim in _BREADTH_CONTEXT_DIMENSIONS
        },
        "observation_breadth": {
            dim: int(problem_breadth.get(dim, 0)) for dim in _BREADTH_OBSERVATION_DIMENSIONS
        },
        "structurally_constant_dimensions": [
            dim
            for dim in batch.get("structurally_constant_dimensions", [])
            if isinstance(dim, str) and dim in _BREADTH_DIMENSIONS
        ],
    }


def _infer_review_domain(
    *,
    change_surface: dict[str, Any] | None,
    needs_ux_review: bool = False,
) -> str:
    surface = change_surface if isinstance(change_surface, dict) else {}
    kinds = set(_coerce_string_list(surface.get("kinds")))
    if kinds & {"new_command", "new_top_level_mode", "new_config_schema", "new_flag"}:
        return _REVIEW_DOMAIN_COMMAND_SURFACE
    if kinds & {"breaking_change", "new_api", "behavior_change"}:
        return _REVIEW_DOMAIN_BEHAVIOR_COMPAT
    if needs_ux_review:
        return _REVIEW_DOMAIN_COMMAND_SURFACE
    return _REVIEW_DOMAIN_BEHAVIOR_COMPAT


def _ticket_owner_component(ticket: dict[str, Any]) -> str | None:
    """
    Return normalized owner/component label used for routing decisions.
    """

    owner = _coerce_string(ticket.get("suggested_owner")) or _coerce_string(ticket.get("component"))
    if owner is None:
        return None
    return owner.strip().lower()


def _severity_rank(value: str) -> int:
    """Handle severity rank processing.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    int
        Process exit code.
    """
    return _EXPORT_SEVERITY_ORDER.get(value, _EXPORT_SEVERITY_ORDER["medium"])


def _safe_relpath(path: Path, root: Path) -> str:
    """
    Return a stable forward-slash relative path for JSON artifacts.

    Parameters
    ----------
    path:
        Filesystem path to represent.
    root:
        Root directory to relativize against.

    Returns
    -------
    str
        Relative path (preferred) or a best-effort stringified path.
    """

    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except (OSError, RuntimeError, ValueError):
        return str(path).replace("\\", "/")


def _is_remote_repo_input(value: str) -> bool:
    """Return whether the value is remote repo input.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    bool
        Boolean decision result.
    """
    candidate = value.strip()
    if not candidate:
        return False
    if "://" in candidate:
        return True
    return candidate.startswith("git@")


def _normalize_remote_repo_input_for_match(value: str) -> str:
    """
    Normalize a remote repo input string for fuzzy matching against git remote URLs.

    Examples
    --------
    - https://github.com/org/repo.git -> github.com/org/repo
    - git@github.com:org/repo.git -> github.com/org/repo
    """

    raw = value.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[: -len(".git")]

    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or parsed.netloc or "").strip().lower()
        path = (parsed.path or "").strip().strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        if host and path:
            return f"{host}/{path.lower()}"
        return (host or raw).lower()

    match = re.match(r"^(?P<user>[^@]+)@(?P<host>[^:]+):(?P<path>.+)$", raw)
    if match is not None:
        host = match.group("host").strip().lower()
        path = match.group("path").strip().strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        if host and path:
            return f"{host}/{path.lower()}"
        return (host or raw).lower()

    return raw.lower()


def _git_remote_urls(repo_root: Path) -> set[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "-v"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return set()

    urls: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        url = parts[1].strip()
        if url:
            urls.add(url)
    return urls


def _remote_repo_input_matches_repo_root(*, repo_input: str, repo_root: Path) -> bool:
    if not _is_remote_repo_input(repo_input):
        return False
    target = _normalize_remote_repo_input_for_match(repo_input)
    for url in _git_remote_urls(repo_root):
        if _normalize_remote_repo_input_for_match(url) == target:
            return True
    return False


def _resolve_local_repo_input_root(*, repo_input: str | None, repo_root: Path) -> Path | None:
    """
    Resolve a local filesystem repo_input to an existing directory, if possible.
    """

    if repo_input is None:
        return None
    if _is_remote_repo_input(repo_input):
        return None
    root_candidate = Path(repo_input)
    if not root_candidate.is_absolute():
        root_candidate = (repo_root / root_candidate).resolve()
    else:
        root_candidate = root_candidate.resolve()
    if not root_candidate.exists() or not root_candidate.is_dir():
        return None
    return root_candidate


def _resolve_owner_repo_root(
    *,
    ticket: dict[str, Any],
    scope_repo_input: str | None,
    cli_repo_input: str | None,
    repo_root: Path,
) -> tuple[Path, str | None, str]:
    """
    Resolve the owner repository root for a ticket export.

    Resolution precedence:
    1) Monorepo component owner (`runner_core`, `agent_adapters`, `sandbox_runner`)
       -> route to `--repo-root`.
    2) `ticket["repo_inputs_citing"]` (single unique entry)
    3) backlog scope repo_input
    4) CLI `--repo-input`
    5) `--repo-root` fallback (loud, explicit)
    """

    owner_component = _ticket_owner_component(ticket)
    if owner_component in _MONOREPO_OWNER_COMPONENTS:
        return repo_root, str(repo_root), f"suggested_owner:{owner_component}"

    ticket_repo_inputs = sorted(set(_coerce_string_list(ticket.get("repo_inputs_citing"))))
    source_label = "ticket_repo_inputs_citing"
    chosen: str | None = None

    if ticket_repo_inputs:
        if len(ticket_repo_inputs) > 1:
            # Some historical runs captured Windows paths with redundant separators
            # (e.g., `I:\\\\code\\\\...`) that show up as distinct strings. If all
            # candidates resolve to the same local dir (or to the current repo via a matching
            # remote), treat them as one owner.
            resolved_owner_keys: dict[str, str] = {}
            all_resolvable = True
            for raw in ticket_repo_inputs:
                root = _resolve_local_repo_input_root(repo_input=raw, repo_root=repo_root)
                if root is None and _remote_repo_input_matches_repo_root(
                    repo_input=raw,
                    repo_root=repo_root,
                ):
                    root = repo_root
                if root is None:
                    all_resolvable = False
                    break
                try:
                    key = os.path.normcase(str(root.resolve()))
                except (OSError, RuntimeError):
                    key = os.path.normcase(str(root))
                resolved_owner_keys[key] = str(root)

            if all_resolvable and len(resolved_owner_keys) == 1:
                chosen = next(iter(resolved_owner_keys.values()))
                source_label = "ticket_repo_inputs_citing_normalized"
            else:
                fingerprint = ticket_export_fingerprint(ticket)
                raise ValueError(
                    "Ticket has multiple owning repo candidates; "
                    "split backlog by repo_input first. "
                    f"fingerprint={fingerprint} repo_inputs={ticket_repo_inputs}"
                )
        if chosen is None:
            chosen = ticket_repo_inputs[0]
    elif scope_repo_input is not None:
        source_label = "backlog_scope_repo_input"
        chosen = scope_repo_input
    elif cli_repo_input is not None:
        source_label = "cli_repo_input"
        chosen = cli_repo_input

    if chosen is None:
        fingerprint = ticket_export_fingerprint(ticket)
        print(
            "WARNING: ticket has no repo_input context; "
            f"defaulting owner repo to --repo-root for fingerprint {fingerprint}.",
            file=sys.stderr,
        )
        return repo_root, None, "repo_root_fallback"

    if _is_remote_repo_input(chosen):
        fingerprint = ticket_export_fingerprint(ticket)
        if _remote_repo_input_matches_repo_root(repo_input=chosen, repo_root=repo_root):
            return repo_root, str(repo_root), f"repo_root_remote_match:{source_label}"
        raise ValueError(
            "Cannot write idea file for remote repo_input. "
            f"fingerprint={fingerprint} repo_input={chosen}"
        )

    root_candidate = Path(chosen)
    if not root_candidate.is_absolute():
        root_candidate = (repo_root / root_candidate).resolve()
    else:
        root_candidate = root_candidate.resolve()

    if not root_candidate.exists() or not root_candidate.is_dir():
        fingerprint = ticket_export_fingerprint(ticket)
        raise ValueError(
            "Owning repo path does not exist or is not a directory. "
            f"fingerprint={fingerprint} repo_input={chosen} resolved={root_candidate}"
        )
    return root_candidate, chosen, source_label


def _read_text_excerpt(path: Path, *, max_bytes: int) -> str:
    """
    Read up to `max_bytes` bytes from a UTF-8-ish text file and return a decoded excerpt.

    Parameters
    ----------
    path:
        File path to read.
    max_bytes:
        Maximum number of bytes to read.

    Returns
    -------
    str
        Decoded excerpt (may be truncated).

    Raises
    ------
    FileNotFoundError
        If `path` does not exist.
    OSError
        If reading fails.
    """

    max_bytes = max(1, int(max_bytes))
    with path.open("rb") as handle:
        data = handle.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def _extract_markdown_title(text: str) -> str | None:
    """Extract markdown title from input content.

    Parameters
    ----------
    text:
        Input text payload.

    Returns
    -------
    str | None
        Computed return value.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            return title if title else None
    return None


def _index_docs(*, repo_root: Path, docs_dir: Path, max_doc_bytes: int) -> list[dict[str, Any]]:
    """
    Create a lightweight index of markdown files under `docs_dir`.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.
    docs_dir:
        Directory containing docs (often `<repo_root>/docs`).
    max_doc_bytes:
        Maximum bytes to read from each file when extracting a title.

    Returns
    -------
    list[dict[str, Any]]
        List of docs entries with `path`, `size_bytes`, and `title` when available.
    """

    if not docs_dir.exists() or not docs_dir.is_dir():
        return []

    entries: list[dict[str, Any]] = []
    try:
        paths = sorted(p for p in docs_dir.rglob("*.md") if p.is_file())
    except OSError:
        return []

    for path in paths:
        try:
            size_bytes = int(path.stat().st_size)
        except OSError:
            continue
        try:
            excerpt = _read_text_excerpt(path, max_bytes=max_doc_bytes)
        except OSError:
            excerpt = ""
        title = _extract_markdown_title(excerpt) if excerpt else None
        entries.append(
            {
                "path": _safe_relpath(path, repo_root),
                "size_bytes": size_bytes,
                "title": title,
            }
        )
    return entries


def _parser_option_strings(parser: argparse.ArgumentParser) -> list[str]:
    """
    Extract a sorted list of option strings (flags) from an argparse parser.

    Parameters
    ----------
    parser:
        The parser to introspect.

    Returns
    -------
    list[str]
        Sorted unique option strings for this command parser.
    """

    options: set[str] = set()
    for action in getattr(parser, "_actions", []):
        option_strings = getattr(action, "option_strings", None)
        if not isinstance(option_strings, list):
            continue
        for opt in option_strings:
            if isinstance(opt, str) and opt.startswith("-") and opt not in {"-h", "--help"}:
                options.add(opt)
    return sorted(options)


def _extract_cli_commands(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """
    Machine-extract the CLI command surface from an argparse parser tree.

    Parameters
    ----------
    parser:
        Root argparse parser (typically from `build_parser()`).

    Returns
    -------
    list[dict[str, Any]]
        Command entries, including intermediate groups and leaf commands.
    """

    prog = _coerce_string(getattr(parser, "prog", None)) or "usertest"
    commands: list[dict[str, Any]] = []

    def _walk(current: argparse.ArgumentParser, words: list[str], help_text: str | None) -> None:
        """Walk parser/action trees and collect lint findings.

        Parameters
        ----------
        current:
            Current parser/action node.
        words:
            Collected name words.
        help_text:
            Help text string for parser action.

        Returns
        -------
        None
            None.
        """
        sub_actions = [
            action
            for action in getattr(current, "_actions", [])
            if isinstance(action, argparse._SubParsersAction)
        ]
        has_subcommands = bool(sub_actions)

        if words:
            commands.append(
                {
                    "command": " ".join([prog, *words]),
                    "help": help_text,
                    "is_group": has_subcommands,
                    "options": _parser_option_strings(current),
                }
            )

        for sub_action in sub_actions:
            for name, subparser in sorted(sub_action.choices.items(), key=lambda kv: kv[0]):
                if not isinstance(name, str) or not isinstance(subparser, argparse.ArgumentParser):
                    continue
                sub_help = _coerce_string(getattr(subparser, "description", None))
                _walk(subparser, [*words, name], sub_help)

    _walk(parser, [], None)
    return commands


def _render_template(template: str, replacements: dict[str, str]) -> str:
    """Render template output text.

    Parameters
    ----------
    template:
        Template text input.
    replacements:
        Template replacement mapping.

    Returns
    -------
    str
        Normalized string result.
    """
    out = template
    for key, value in replacements.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def _parse_first_json_object(raw_text: str) -> dict[str, Any] | None:
    """Parse first json object from input text.

    Parameters
    ----------
    raw_text:
        Raw text payload.

    Returns
    -------
    dict[str, Any] | None
        Structured mapping result.
    """
    text = raw_text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _summarize_atoms_for_totals(atoms: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize atoms for totals into aggregate counters.

    Parameters
    ----------
    atoms:
        Backlog atom payload list.

    Returns
    -------
    dict[str, Any]
        Structured mapping result.
    """
    source_counts: dict[str, int] = {}
    severity_hint_counts: dict[str, int] = {}
    runs: set[str] = set()
    for atom in atoms:
        run_rel = _coerce_string(atom.get("run_rel"))
        if run_rel is not None and not run_rel.startswith("__aggregate__/"):
            runs.add(run_rel)
        source = _coerce_string(atom.get("source"))
        if source is not None:
            source_counts[source] = source_counts.get(source, 0) + 1
        severity = _coerce_string(atom.get("severity_hint")) or "medium"
        severity_hint_counts[severity] = severity_hint_counts.get(severity, 0) + 1
    return {
        "runs": len(runs),
        "atoms": len(atoms),
        "source_counts": source_counts,
        "severity_hint_counts": severity_hint_counts,
    }


__all__ = [name for name in globals() if not name.startswith("__")]
