"""Bind model-authored research claims to retained runner evidence.

The stage-3 report is authored by an agent, so schema validation alone cannot
establish that a command ran, a file was inspected, or an artifact exists.  This
module produces a runner-owned receipt from the acquired workspace and normalized
events.  Downstream readiness requires a successful receipt.
"""

from __future__ import annotations

import ast
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

import yaml
from agent_adapters.read_attestation import observed_read_attestation
from backlog_core.causal_proof import (
    command_authorization_errors,
    command_authorization_identity,
    content_bound_payload,
    evaluate_proof_predicate,
    proof_predicate_contract_errors,
    proof_receipt_id_for,
    validate_causal_proof_receipt,
)
from backlog_core.stage_contracts import (
    evidence_assignment_sha256,
    evidence_verification_sha256,
    replay_invocation_references_model_overlay,
    research_claims_sha256,
)
from runner_core.codex_execpolicy import verify_controlled_codex_execpolicy_receipt
from runner_core.target_acquire import acquire_target
from sandbox_runner import DockerSandbox, SandboxSpec

from backlog_miner.origin_evidence import (
    origin_attachment_requirements,
    verify_materialized_origin_attachments,
)
from backlog_miner.proof_adapters import (
    PositiveBasisContext,
    ProofAdapterContext,
    builtin_positive_basis_registry,
    builtin_proof_adapter_registry,
)
from backlog_miner.proof_adapters.base import (
    environment_attestation,
    replay_environment_attestation,
)

_VERIFICATION_METHOD = "runner_artifact_binding_v1"
_REPLAY_INLINE_CODE_FLAGS = frozenset(
    {"-c", "--command", "-e", "--eval", "--execute", "/c", "-command"}
)
_PYTEST_ARGV_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("pytest",),
    ("python", "-m", "pytest"),
    ("pdm", "run", "pytest"),
    ("pdm", "run", "python", "-m", "pytest"),
)
_PYTEST_AMBIGUOUS_SELECTION_OPTIONS = frozenset(
    {
        "-k",
        "--keyword",
        "-m",
        "--markexpr",
        "--pyargs",
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--collect-only",
        "--co",
    }
)
_REPLAY_FORBIDDEN_CHARACTERS = frozenset("&|;<>`")
_MECHANISM_EVIDENCE_TYPES = frozenset(
    {
        "exception_trace",
        "observed_output",
        "controlled_scenario",
        "temporary_harness",
        "static_trace",
        "live_runtime",
    }
)
_FALSIFICATION_ATTEMPT_OUTCOMES = frozenset({"survived", "disproved", "inconclusive"})
_FALSIFICATION_REPLAY_SCENARIOS = frozenset(
    {"original_replay", "faithful_replay", "control", "static_trace", "live_runtime"}
)
_SENSITIVE_ENVIRONMENT_RE = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE|SESSION|"
    r"^AWS_|^AZURE_|^GOOGLE_|^GCP_|^GH_|^GITHUB_|^SSH_)",
    re.IGNORECASE,
)
_REPLAY_ENVIRONMENT_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REPLAY_FORBIDDEN_ENVIRONMENT_KEYS = frozenset(
    {
        "COMSPEC",
        "HOME",
        "PATH",
        "PATHEXT",
        "PYTHONHOME",
        "PYTHONPATH",
        "SHELL",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
)
_REPLAY_FORBIDDEN_ENVIRONMENT_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CODEX_",
    "GITHUB_",
    "GOOGLE_",
    "OPENAI_",
)
_IGNORED_WORKSPACE_DIRS = frozenset(
    {".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__"}
)
_RUNNER_WORKSPACE_FILES = frozenset({"system_prompt.md", "append_system_prompt.md"})


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _expected_semantic_field_path(value: Any) -> bool:
    """Return whether an immutable field is objectively named as desired behavior."""

    if not isinstance(value, str):
        return False
    names = re.findall(r"\.([A-Za-z_][A-Za-z0-9_:-]*)", value)
    if not names:
        return False
    terminal = names[-1].casefold()
    exact = terminal in {
        "expected",
        "desired",
        "correct_value",
        "expected_behavior",
        "desired_behavior",
        "intended_behavior",
        "required_behavior",
        "success_criteria",
    }
    prefixed = terminal.startswith(("expected_", "desired_", "correct_", "intended_", "required_"))
    # Planning/proposal metadata describes a hoped-for change, not an observed
    # interface contract.  In particular, historical atoms commonly contain
    # ``suggested_change.expected_impact``; accepting it would turn a proposal
    # into a runner-grounded success criterion.
    proposal_tokens = {
        "actual",
        "impact",
        "effect",
        "benefit",
        "context",
        "diagnostic",
        "error",
        "failure",
        "observed",
        "risk",
        "symptom",
        "change",
        "fix",
        "solution",
        "implementation",
        "plan",
        "estimate",
        "proposal",
        "recommendation",
    }
    return (exact or prefixed) and not any(
        token in proposal_tokens for token in terminal.split("_")
    )


def _source_observation_atom(snapshot: Any) -> bool:
    """Return whether a snapshot is original observation evidence, not commentary."""

    if not isinstance(snapshot, dict) or snapshot.get("evidence_role") != "observation":
        return False
    if str(snapshot.get("origin_stage") or "").casefold() in {
        "repro_research",
        "research",
        "implementation",
        "verification",
    }:
        return False
    proposal_kinds = {
        "idea",
        "proposal",
        "recommendation",
        "suggested_change",
        "suggestion",
    }
    return not any(
        str(snapshot.get(field) or "").casefold() in proposal_kinds
        for field in ("category", "kind", "source", "surface_kind")
    )


def _semantic_quote_field_path(value: Any) -> bool:
    """Allow source narrative fields while rejecting symptom/proposal metadata."""

    if not isinstance(value, str):
        return False
    names = re.findall(r"\.([A-Za-z_][A-Za-z0-9_:-]*)", value)
    if not names:
        return False
    terminal = names[-1].casefold()
    forbidden = {
        "actual",
        "context",
        "error",
        "impact",
        "observed",
        "proposal",
        "recommendation",
        "suggested_change",
        "symptom",
    }
    return not any(token in forbidden for token in terminal.split("_"))


def _expectation_quote(value: Any, *, expected_value: Any) -> bool:
    """Validate quote presence only; semantic sufficiency is judged at stage 5."""

    del expected_value
    return isinstance(value, str) and bool(value.strip())


def _falsification_assertion_relation(
    disproof_condition: Mapping[str, Any],
    observed_assertion: Mapping[str, Any],
    *,
    outcome: str,
) -> bool:
    """Return whether the replay result establishes the declared challenge outcome."""

    if outcome == "disproved":
        return dict(observed_assertion) == dict(disproof_condition)
    if outcome == "inconclusive":
        return True
    if outcome != "survived":
        return False
    source = disproof_condition.get("source")
    operator = disproof_condition.get("operator")
    expected = disproof_condition.get("expected")
    if observed_assertion.get("source") != source:
        return False
    observed_operator = observed_assertion.get("operator")
    observed_expected = observed_assertion.get("expected")
    if operator == "contains":
        return observed_operator == "not_contains" and observed_expected == expected
    if operator == "not_contains":
        return observed_operator == "contains" and observed_expected == expected
    if operator != "equals" or observed_operator != "equals":
        return False
    if source == "exit_code":
        return (
            isinstance(expected, int)
            and not isinstance(expected, bool)
            and isinstance(observed_expected, int)
            and not isinstance(observed_expected, bool)
            and observed_expected != expected
        )
    return (
        isinstance(expected, str)
        and isinstance(observed_expected, str)
        and observed_expected != expected
    )


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalize_command(value: str) -> str:
    return " ".join(value.split())


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").strip().removeprefix("./").casefold()


def _read_workspace(run_dir: Path) -> Path | None:
    ref_path = run_dir / "workspace_ref.json"
    if not ref_path.exists():
        return None
    try:
        raw = json.loads(ref_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    workspace_raw = _text(raw.get("workspace_dir"))
    return Path(workspace_raw).resolve() if workspace_raw is not None else None


def _workspace_head(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _workspace_clean(workspace: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return not result.stdout.strip() if result.returncode == 0 else None


def materialize_clean_revision_view(
    *,
    source_workspace: Path,
    destination: Path,
    repo_revision: str,
) -> tuple[Path | None, str | None, bool | None, list[str]]:
    """Create or verify a clean clone fixed at the researched revision."""
    errors: list[str] = []
    destination = destination.resolve()
    if not destination.exists():
        try:
            acquired = acquire_target(
                repo=str(source_workspace),
                dest_dir=destination,
                ref=repo_revision,
            )
            destination = acquired.workspace_dir.resolve()
        except FileExistsError as exc:
            # On Windows ``acquire_target`` may relocate a long destination into the
            # shared temp workspace root.  The caller's requested path can therefore
            # remain absent while the effective relocated path already exists from an
            # earlier verification.  Reuse that path and verify its revision/cleanliness
            # below instead of misclassifying deterministic reuse as an acquisition
            # failure.
            existing_raw = exc.filename or (exc.args[0] if exc.args else None)
            existing = Path(existing_raw).resolve() if existing_raw is not None else None
            if existing is None or not existing.is_dir():
                return None, None, None, [
                    "planning_revision_view_acquire_failed:FileExistsError"
                ]
            destination = existing
        except (OSError, RuntimeError, ValueError) as exc:
            return None, None, None, [f"planning_revision_view_acquire_failed:{type(exc).__name__}"]
    if not destination.is_dir():
        return None, None, None, ["planning_revision_view_not_directory"]
    head = _workspace_head(destination)
    clean = _workspace_clean(destination)
    if head is None:
        errors.append("planning_revision_view_head_unverifiable")
    elif head.casefold() != repo_revision.casefold():
        errors.append(f"planning_revision_view_head_mismatch:{head}:{repo_revision}")
    if clean is not True:
        errors.append("planning_revision_view_not_clean")
    return destination, head, clean, errors


def _git_output_bytes(workspace: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _workspace_manifest(workspace: Path) -> dict[str, dict[str, Any]]:
    """Return a canonical, non-following manifest of observable workspace entries."""
    manifest: dict[str, dict[str, Any]] = {}
    pending = [workspace]
    while pending:
        parent = pending.pop()
        try:
            entries = list(os.scandir(parent))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                relative = path.relative_to(workspace)
            except ValueError:
                continue
            if any(part in _IGNORED_WORKSPACE_DIRS for part in relative.parts):
                continue
            try:
                metadata = path.lstat()
            except OSError:
                continue
            relative_key = relative.as_posix()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(path)
                except OSError:
                    target = "<unreadable>"
                manifest[relative_key] = {
                    "kind": "symlink",
                    "mode": mode,
                    "target": target,
                }
            elif stat.S_ISREG(metadata.st_mode):
                manifest[relative_key] = {
                    "kind": "file",
                    "mode": mode,
                    "sha256": _sha256_path(path),
                    "size_bytes": metadata.st_size,
                }
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            else:
                manifest[relative_key] = {"kind": "other", "mode": mode}
    return dict(sorted(manifest.items()))


def _canonical_workspace_state(workspace: Path) -> dict[str, Any]:
    index_stage = _git_output_bytes(workspace, "ls-files", "--stage", "-z")
    index_flags = _git_output_bytes(workspace, "ls-files", "-v", "-z")
    return {
        "head": _workspace_head(workspace),
        "entries": _workspace_manifest(workspace),
        "git_index_stage_sha256": (
            sha256(index_stage).hexdigest() if index_stage is not None else None
        ),
        "git_index_flags_sha256": (
            sha256(index_flags).hexdigest() if index_flags is not None else None
        ),
    }


def _declared_disposable_state_paths(
    experiment: Mapping[str, Any],
    *,
    workspace: Path,
    experiment_id: str,
    errors: list[str],
) -> list[str]:
    setup = experiment.get("replay_setup")
    raw = setup.get("disposable_state_paths") if isinstance(setup, Mapping) else None
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw or len(raw) > 32:
        errors.append(f"experiment_replay_state_paths_invalid:{experiment_id}")
        return []
    paths: list[str] = []
    root = workspace.resolve()
    for item in raw:
        normalized = str(item).replace("\\", "/") if isinstance(item, str) else ""
        relative = PurePosixPath(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.parts[0].casefold() == ".git"
        ):
            errors.append(f"experiment_replay_state_path_forbidden:{experiment_id}:{item}")
            continue
        resolved = (root / Path(*relative.parts)).resolve()
        if not _within(resolved, root):
            errors.append(f"experiment_replay_state_path_forbidden:{experiment_id}:{item}")
            continue
        parent = resolved
        symlink_parent = False
        while parent != root:
            if parent.exists() and parent.is_symlink():
                symlink_parent = True
                break
            parent = parent.parent
        tracked = _git_output_bytes(workspace, "ls-files", "-z", "--", relative.as_posix())
        if symlink_parent or (tracked is not None and bool(tracked)):
            errors.append(f"experiment_replay_state_path_not_disposable:{experiment_id}:{item}")
            continue
        paths.append(relative.as_posix())
    return list(dict.fromkeys(paths))


def _state_entry_is_declared(relative: str, declared: Sequence[str]) -> bool:
    path = PurePosixPath(relative)
    return any(
        path == PurePosixPath(root) or PurePosixPath(root) in path.parents
        for root in declared
    )


def _declared_state_transition_receipts(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    declared: Sequence[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    before_entries = before.get("entries") if isinstance(before.get("entries"), Mapping) else {}
    after_entries = after.get("entries") if isinstance(after.get("entries"), Mapping) else {}
    changed = sorted(
        path
        for path in set(before_entries) | set(after_entries)
        if before_entries.get(path) != after_entries.get(path)
    )
    receipts: list[dict[str, Any]] = []
    for root in declared:
        root_before = {
            path: value
            for path, value in before_entries.items()
            if _state_entry_is_declared(path, [root])
        }
        root_after = {
            path: value
            for path, value in after_entries.items()
            if _state_entry_is_declared(path, [root])
        }
        receipt = {
            "path": root,
            "runner_attested": True,
            "before_entries": root_before,
            "after_entries": root_after,
            "changed_entries": [
                path for path in changed if _state_entry_is_declared(path, [root])
            ],
        }
        receipt["transition_sha256"] = _canonical_json_sha256(receipt)
        receipts.append(receipt)
    undeclared = [path for path in changed if not _state_entry_is_declared(path, declared)]
    return undeclared, receipts


def _workspace_overlay_errors(
    *,
    research_workspace: Path,
    baseline_workspace: Path,
) -> tuple[list[str], dict[str, Any]]:
    research_state = _canonical_workspace_state(research_workspace)
    baseline_state = _canonical_workspace_state(baseline_workspace)
    research_manifest = research_state["entries"]
    baseline_manifest = baseline_state["entries"]
    errors: list[str] = []
    changed_baseline = sorted(
        path for path, entry in baseline_manifest.items() if research_manifest.get(path) != entry
    )
    extras = sorted(set(research_manifest) - set(baseline_manifest))
    all_overlay_paths = [path for path in extras if path.startswith(".usertest_research/")]
    runner_materialized_paths = [
        path for path in all_overlay_paths if path.startswith(".usertest_research/origin_evidence/")
    ]
    overlay_paths = [path for path in all_overlay_paths if path not in runner_materialized_paths]
    unsafe_overlay_paths = [
        path for path in all_overlay_paths if research_manifest.get(path, {}).get("kind") != "file"
    ]
    suspicious_extras = [
        path
        for path in extras
        if path not in _RUNNER_WORKSPACE_FILES and path not in all_overlay_paths
    ]
    suspicious_extras.extend(unsafe_overlay_paths)
    suspicious_extras = sorted(set(suspicious_extras))
    index_changed = any(
        research_state.get(field) != baseline_state.get(field)
        for field in ("git_index_stage_sha256", "git_index_flags_sha256")
    )
    if changed_baseline:
        errors.extend(f"baseline_file_changed:{path}" for path in changed_baseline)
    if suspicious_extras:
        errors.extend(f"untracked_workspace_file:{path}" for path in suspicious_extras)
    if index_changed:
        errors.append("git_index_changed")
    overlay_manifest = {
        path: research_manifest[path]
        for path in all_overlay_paths
        if path not in unsafe_overlay_paths
    }
    return errors, {
        "baseline_manifest_sha256": _canonical_json_sha256(baseline_manifest),
        "research_manifest_sha256": _canonical_json_sha256(research_manifest),
        "baseline_state_sha256": _canonical_json_sha256(baseline_state),
        "research_state_sha256": _canonical_json_sha256(research_state),
        "baseline_git_index_sha256": _canonical_json_sha256(
            {
                "stage": baseline_state.get("git_index_stage_sha256"),
                "flags": baseline_state.get("git_index_flags_sha256"),
            }
        ),
        "research_git_index_sha256": _canonical_json_sha256(
            {
                "stage": research_state.get("git_index_stage_sha256"),
                "flags": research_state.get("git_index_flags_sha256"),
            }
        ),
        "changed_baseline_paths": changed_baseline,
        "research_overlay_paths": overlay_paths,
        "runner_materialized_evidence_paths": runner_materialized_paths,
        "research_overlay_manifest": overlay_manifest,
        "research_overlay_manifest_sha256": _canonical_json_sha256(overlay_manifest),
        "suspicious_extra_paths": suspicious_extras,
        "git_index_changed": index_changed,
    }


def _verified_diff_classification(
    prior: Any,
    workspace_overlay: dict[str, Any],
) -> str:
    """Never downgrade a suspicious runner/model classification after replay."""
    if (
        prior == "suspicious_implementation"
        or workspace_overlay.get("changed_baseline_paths")
        or workspace_overlay.get("suspicious_extra_paths")
        or workspace_overlay.get("git_index_changed") is True
    ):
        return "suspicious_implementation"
    if workspace_overlay.get("research_overlay_paths"):
        return "allowed_research_edits"
    return "no_changes"


def _parse_argv_without_shell(command: str) -> list[str] | None:
    """Parse one command as argv while rejecting every shell/control boundary."""
    if not command or any(ord(character) < 32 or ord(character) == 127 for character in command):
        return None
    if any(character in _REPLAY_FORBIDDEN_CHARACTERS for character in command):
        return None
    if re.search(r"\\\s", command):
        # Backslash-escaped whitespace is shell syntax, but replay executes with
        # shell=False. Require a quoted argument instead of silently changing argv.
        return None
    try:
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        # Preserve Windows path separators. Quotes still group/strip normally,
        # while backslash never becomes an implicit POSIX shell escape.
        lexer.escape = ""
        argv = list(lexer)
    except ValueError:
        return None
    return argv if argv and all(argv) else None


def _portable_replay_path_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Normalize only positional Python-harness and pytest path arguments."""

    portable = list(argv)
    normalized = tuple(token.casefold() for token in portable)
    path_indexes: list[int] = []
    pytest_paths = False
    if normalized[:3] == ("pdm", "run", "python") and normalized[3:5] != (
        "-m",
        "pytest",
    ):
        if len(portable) > 3:
            path_indexes = [3]
    elif normalized[:1] == ("python",) and normalized[1:3] != ("-m", "pytest"):
        if len(portable) > 1:
            path_indexes = [1]
    else:
        for prefix in _PYTEST_ARGV_PREFIXES:
            if normalized[: len(prefix)] == prefix:
                pytest_paths = True
                path_indexes = [
                    index
                    for index in range(len(prefix), len(portable))
                    if not portable[index].startswith("-")
                ]
                break

    changed = False
    for index in path_indexes:
        token = portable[index]
        if "\\" not in token:
            continue
        if pytest_paths:
            path_token, selector_separator, selector = token.partition("::")
            normalized_path = path_token.replace("\\", "/")
            path_part = normalized_path.casefold()
            if not (
                path_part.endswith(".py")
                or path_part.startswith((".usertest_research/", "apps/", "packages/", "tests/"))
            ):
                continue
            portable[index] = normalized_path + selector_separator + selector
        else:
            portable[index] = token.replace("\\", "/")
        changed = True
    return portable, changed


def _parse_replay_argv(command: str) -> list[str] | None:
    """Parse one direct-argv command without a shell or inline-code escape hatch.

    The research report is model-authored.  Normalizing whitespace before an
    allowlist check is unsafe because PowerShell and POSIX shells treat newlines
    as command separators.  Reject every control/shell character in the raw
    string, parse it once, and execute only the resulting argv with ``shell=False``.
    """
    argv = _parse_argv_without_shell(command)
    if argv is None:
        return None
    argv, _portable_path_changed = _portable_replay_path_argv(argv)
    normalized = tuple(token.casefold() for token in argv)
    if any(token in _REPLAY_INLINE_CODE_FLAGS for token in normalized[1:]):
        return None
    # Executable and language policy is intentionally not encoded here. The
    # authorization step below must bind at least one argv entrypoint to the
    # immutable repository or to an attested research overlay. Inline command
    # flags remain forbidden for every runtime.
    return argv


def _repo_file(workspace: Path, raw: str) -> Path | None:
    candidate = Path(raw.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = (workspace / candidate).resolve()
    if not _within(resolved, workspace.resolve()) or not resolved.is_file():
        return None
    return resolved


def _python_module_entrypoint(workspace: Path, module: str) -> Path | None:
    if (
        not module
        or module.startswith(".")
        or any(not part or not part.replace("_", "a").isalnum() for part in module.split("."))
    ):
        return None
    relative = Path(*module.split("."))
    candidates = (
        relative.with_suffix(".py"),
        relative / "__main__.py",
        Path("src") / relative.with_suffix(".py"),
        Path("src") / relative / "__main__.py",
    )
    return next(
        (resolved for item in candidates if (resolved := _repo_file(workspace, str(item)))),
        None,
    )


def _project_script_entrypoint(workspace: Path, script_name: str) -> dict[str, Any] | None:
    """Resolve one immutable project-declared console/task script without importing it."""
    pyproject_path = workspace / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            parsed = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            parsed = {}
        project = parsed.get("project") if isinstance(parsed, dict) else None
        scripts = project.get("scripts") if isinstance(project, dict) else None
        target = scripts.get(script_name) if isinstance(scripts, dict) else None
        if isinstance(target, str) and target.strip():
            module = target.split(":", 1)[0].strip()
            source = _python_module_entrypoint(workspace, module)
            if source is not None:
                return {
                    "entrypoint_kind": "project_console_script",
                    "entrypoint_path": source.relative_to(workspace).as_posix(),
                    "declaration_path": "pyproject.toml",
                    "declaration_sha256": _sha256_path(pyproject_path),
                    "script_name": script_name,
                    "declared_target": target,
                }
    return None


def _resolve_repository_entrypoint(argv: list[str], *, workspace: Path) -> dict[str, Any] | None:
    """Resolve practical argv to repository-owned source/config, never PATH alone."""
    effective = list(argv)
    used_project_runner = False
    if tuple(token.casefold() for token in effective[:2]) == ("pdm", "run"):
        effective = effective[2:]
        used_project_runner = True
    if not effective:
        return None
    executable = Path(effective[0]).name.casefold()
    python_names = {"python", "python3", "python.exe", "py", "py.exe"}
    node_names = {"node", "node.exe"}
    receipt: dict[str, Any] | None = None
    if executable in python_names:
        if len(effective) >= 3 and effective[1] == "-m":
            if effective[2].casefold() != "pytest":
                source = _python_module_entrypoint(workspace, effective[2])
                if source is not None:
                    receipt = {
                        "entrypoint_kind": "python_module",
                        "entrypoint_path": source.relative_to(workspace).as_posix(),
                        "module": effective[2],
                    }
        elif len(effective) >= 2 and not effective[1].startswith("-"):
            source = _repo_file(workspace, effective[1])
            if source is not None and source.suffix.casefold() == ".py":
                receipt = {
                    "entrypoint_kind": "python_script",
                    "entrypoint_path": source.relative_to(workspace).as_posix(),
                }
    elif executable in node_names and len(effective) >= 2 and not effective[1].startswith("-"):
        source = _repo_file(workspace, effective[1])
        if source is not None and source.suffix.casefold() in {".js", ".cjs", ".mjs"}:
            receipt = {
                "entrypoint_kind": "node_script",
                "entrypoint_path": source.relative_to(workspace).as_posix(),
            }
    elif executable in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}:
        if len(effective) >= 3 and effective[1].casefold() == "run":
            package_path = workspace / "package.json"
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                package = None
            scripts = package.get("scripts") if isinstance(package, dict) else None
            if isinstance(scripts, dict) and isinstance(scripts.get(effective[2]), str):
                receipt = {
                    "entrypoint_kind": "package_script",
                    "entrypoint_path": "package.json",
                    "declaration_path": "package.json",
                    "declaration_sha256": _sha256_path(package_path),
                    "script_name": effective[2],
                    "declared_target_sha256": _canonical_json_sha256(scripts[effective[2]]),
                }
    else:
        project_script = _project_script_entrypoint(workspace, effective[0])
        if project_script is not None:
            receipt = project_script
        else:
            source = _repo_file(workspace, effective[0])
            if source is not None:
                receipt = {
                    "entrypoint_kind": "repository_executable",
                    "entrypoint_path": source.relative_to(workspace).as_posix(),
                }
    if receipt is None:
        for index, token in enumerate(effective):
            if token.startswith("-") and "=" not in token:
                continue
            candidate = token.split("=", 1)[-1].partition("::")[0]
            source = _repo_file(workspace, candidate)
            if source is None:
                continue
            receipt = {
                "entrypoint_kind": "repository_argv_entrypoint",
                "entrypoint_path": source.relative_to(workspace).as_posix(),
                "entrypoint_argv_index": index,
                "runtime_executable": Path(effective[0]).name,
            }
            break
    if receipt is None:
        return None
    entrypoint_path = workspace / str(receipt["entrypoint_path"])
    receipt["entrypoint_sha256"] = _sha256_path(entrypoint_path)
    receipt["entrypoint_git_blob_sha"] = _git_blob_sha(
        workspace,
        str(receipt["entrypoint_path"]),
    )
    receipt["project_runner"] = "pdm" if used_project_runner else None
    return receipt


def _command_authorization_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Content-bind one runner authorization without constraining its vocabulary."""

    return content_bound_payload(
        {**dict(value), "runner_attested": True},
        hash_field="authorization_sha256",
    )


def _declared_repository_binding_receipts(
    *,
    experiment: Mapping[str, Any],
    dossier: Mapping[str, Any],
    workspace: Path,
) -> tuple[bool, list[dict[str, Any]]]:
    """Attest model-declared tracked inputs that govern a shell-free command.

    Some repository-native runners (for example Cargo, Go, Maven, Gradle, and
    dotnet) do not put a repository path in argv.  The declaration is therefore
    an open relationship statement, while the runner independently proves that
    every exact path was inspected, exists in the researched revision, and is
    content-bound.  A present but malformed declaration never falls through to a
    weaker authorization route.
    """

    raw = experiment.get("repository_bindings")
    if raw is None:
        return False, []
    if not isinstance(raw, list) or not raw or len(raw) > 32:
        return True, []
    inspected_raw = dossier.get("inspected_files")
    inspected = {
        _normalize_path(path)
        for path in inspected_raw
        if isinstance(path, str) and path.strip()
    } if isinstance(inspected_raw, list) else set()
    receipts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return True, []
        path_raw = _text(item.get("path"))
        relationship = _text(item.get("relationship"))
        if path_raw is None or relationship is None:
            return True, []
        declared = PurePosixPath(path_raw.replace("\\", "/"))
        if declared.is_absolute() or ".." in declared.parts:
            return True, []
        source = _repo_file(workspace, declared.as_posix())
        if source is None or (workspace / Path(*declared.parts)).is_symlink():
            return True, []
        relative = source.relative_to(workspace.resolve()).as_posix()
        if _normalize_path(relative) not in inspected:
            return True, []
        git_blob_sha = _git_blob_sha(workspace, relative)
        if git_blob_sha is None:
            return True, []
        receipts.append(
            content_bound_payload(
                {
                    "path": relative,
                    "relationship": relationship,
                    "file_sha256": _sha256_path(source),
                    "git_blob_sha": git_blob_sha,
                    "runner_attested": True,
                },
                hash_field="repository_binding_sha256",
            )
        )
    receipts.sort(key=lambda item: (str(item["path"]), str(item["relationship"])))
    return True, receipts


def _command_authorization_attested(
    authorization: Any,
    *,
    argv: Sequence[str],
) -> bool:
    """Validate authorization properties and immutable anchors, not kind names."""
    return not command_authorization_errors(authorization, argv=argv)


def _practical_command_authorization(
    *,
    argv: list[str],
    experiment: dict[str, Any],
    dossier: dict[str, Any],
    assignment: dict[str, Any],
    workspace: Path,
) -> dict[str, Any] | None:
    """Authorize one practical CLI only from immutable evidence or an inspected entrypoint."""
    if _text(experiment.get("scenario_kind")) is None:
        return None
    bindings_declared, repository_bindings = _declared_repository_binding_receipts(
        experiment=experiment,
        dossier=dossier,
        workspace=workspace,
    )
    if bindings_declared:
        if not repository_bindings:
            return None
        return _command_authorization_receipt(
            {
                "authorization_kind": "declared_repository_bindings",
                "executed_argv_sha256": _canonical_json_sha256(argv),
                "shell": False,
                "workspace_confined": True,
                "repository_bindings": repository_bindings,
            }
        )
    entrypoint = _resolve_repository_entrypoint(argv, workspace=workspace)
    if entrypoint is None:
        return None
    addressed_raw = experiment.get("addresses_atom_ids")
    addressed = (
        {atom_id for atom_id in addressed_raw if isinstance(atom_id, str)}
        if isinstance(addressed_raw, list)
        else set()
    )
    receipts_raw = assignment.get("atom_receipts")
    for receipt in receipts_raw if isinstance(receipts_raw, list) else []:
        if not isinstance(receipt, dict) or receipt.get("atom_id") not in addressed:
            continue
        snapshot = receipt.get("atom_snapshot")
        atom_command = snapshot.get("command") if isinstance(snapshot, dict) else None
        atom_argv = (
            _parse_argv_without_shell(atom_command) if isinstance(atom_command, str) else None
        )
        if atom_argv == argv:
            return _command_authorization_receipt({
                "authorization_kind": "immutable_source_command",
                "executed_argv_sha256": _canonical_json_sha256(argv),
                "shell": False,
                "workspace_confined": True,
                "origin_atom_id": receipt.get("atom_id"),
                "origin_atom_sha256": receipt.get("atom_sha256"),
                "origin_atom_field_path": "$.command",
                "origin_command_value_sha256": _canonical_json_sha256(atom_command),
                **entrypoint,
            })
    inspected_raw = dossier.get("inspected_files")
    inspected = (
        {_normalize_path(path) for path in inspected_raw if isinstance(path, str) and path.strip()}
        if isinstance(inspected_raw, list)
        else set()
    )
    possible_paths = {
        _normalize_path(str(entrypoint["entrypoint_path"])),
        _normalize_path(str(entrypoint.get("declaration_path") or "")),
    }
    artifacts_raw = dossier.get("artifact_refs")
    artifact = next(
        (
            item
            for item in artifacts_raw
            if isinstance(item, dict)
            and _normalize_path(str(item.get("path") or ""))
            == _normalize_path(str(entrypoint["entrypoint_path"]))
        ),
        None,
    ) if isinstance(artifacts_raw, list) else None
    if (
        _normalize_path(str(entrypoint["entrypoint_path"])).startswith(
            ".usertest_research/"
        )
        and isinstance(artifact, dict)
    ):
        return _command_authorization_receipt({
            "authorization_kind": "attested_research_harness",
            "executed_argv_sha256": _canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            "artifact_id": artifact.get("artifact_id"),
            **entrypoint,
        })
    matched = sorted((possible_paths - {""}) & inspected)
    if matched:
        return _command_authorization_receipt({
            "authorization_kind": "declared_inspected_repository_entrypoint",
            "executed_argv_sha256": _canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            "inspected_entrypoint_path": matched[0],
            **entrypoint,
        })
    if _text(entrypoint.get("entrypoint_git_blob_sha")) is not None:
        return _command_authorization_receipt({
            "authorization_kind": "immutable_repository_entrypoint",
            "executed_argv_sha256": _canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            **entrypoint,
        })
    return None


def _replay_command_allowed(command: str) -> bool:
    return _parse_replay_argv(command) is not None


def _replay_argv_is_workspace_confined(argv: list[str]) -> bool:
    """Reject arguments that can redirect a test runner outside its clean checkout."""
    for token in argv[1:]:
        candidate = token.split("=", 1)[-1] if "=" in token else token
        if "://" in candidate:
            return False
        posix = PurePosixPath(candidate)
        windows = PureWindowsPath(candidate)
        if posix.is_absolute() or windows.anchor:
            return False
        if ".." in posix.parts or ".." in windows.parts:
            return False
    return True


def _authorized_replay_invocation(
    *,
    command: str,
    experiment: dict[str, Any],
    dossier: dict[str, Any],
    assignment: dict[str, Any],
    workspace: Path,
) -> tuple[list[str], dict[str, Any]] | None:
    """Derive one replay argv and its complete runner-owned authorization.

    Commands are accepted only when the shell-free argv is workspace-confined and resolves
    back to immutable source evidence, an attested research harness, or a declared and inspected
    repository entrypoint. Runtime and file extensions are not centrally enumerated. Reusing this
    derivation during persisted receipt validation prevents a practical command from becoming
    unverifiable after JSON serialization while preserving the original safety boundary.
    """

    argv = _parse_replay_argv(command)
    if argv is None:
        argv = _parse_argv_without_shell(command)
        if argv is not None:
            argv, _portable_path_changed = _portable_replay_path_argv(argv)
    if (
        argv is None
        or any(
            token.casefold() in _REPLAY_INLINE_CODE_FLAGS
            for token in argv[1:]
        )
        or not _replay_argv_is_workspace_confined(argv)
    ):
        return None
    authorization = _practical_command_authorization(
        argv=argv,
        experiment=experiment,
        dossier=dossier,
        assignment=assignment,
        workspace=workspace,
    )
    if authorization is None:
        return None
    if not _replay_argv_is_workspace_confined(argv):
        return None
    return argv, authorization


def _sanitized_replay_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if _SENSITIVE_ENVIRONMENT_RE.search(key) is None
    } | {"CI": "1"}


def _replay_environment_overrides(
    experiment: Mapping[str, Any],
    *,
    experiment_id: str,
    errors: list[str],
) -> dict[str, str | None]:
    setup = experiment.get("replay_setup")
    raw = setup.get("environment") if isinstance(setup, Mapping) else None
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or len(raw) > 32:
        errors.append(f"experiment_replay_environment_invalid:{experiment_id}")
        return {}
    overrides: dict[str, str | None] = {}
    for key, value in raw.items():
        normalized = str(key).upper() if isinstance(key, str) else ""
        forbidden = (
            not isinstance(key, str)
            or _REPLAY_ENVIRONMENT_KEY_RE.fullmatch(key) is None
            or normalized in _REPLAY_FORBIDDEN_ENVIRONMENT_KEYS
            or normalized.startswith(_REPLAY_FORBIDDEN_ENVIRONMENT_PREFIXES)
            or _SENSITIVE_ENVIRONMENT_RE.search(key) is not None
        )
        if (
            forbidden
            or (value is not None and not isinstance(value, str))
            or (isinstance(value, str) and (len(value) > 4096 or "\x00" in value))
        ):
            errors.append(f"experiment_replay_environment_forbidden:{experiment_id}:{key}")
            continue
        overrides[key] = value
    return overrides


def _replay_setup_receipt(
    *,
    environment_overrides: Mapping[str, str | None],
    disposable_state_paths: Sequence[str],
) -> dict[str, Any]:
    receipt = {
        "runner_applied": True,
        "environment": {
            key: (
                {"present": False}
                if value is None
                else {
                    "present": True,
                    "value_sha256": sha256(value.encode("utf-8")).hexdigest(),
                }
            )
            for key, value in sorted(environment_overrides.items())
        },
        "disposable_state_paths": list(disposable_state_paths),
    }
    receipt["replay_setup_sha256"] = _canonical_json_sha256(receipt)
    return receipt


def _replay_inputs_receipt(
    *,
    source_experiment_id: str,
    environment_overrides: Mapping[str, str | None],
    disposable_state_paths: Sequence[str],
) -> dict[str, Any]:
    """Retain the exact, already-sanitized inputs needed to replay the source case.

    The public setup receipt remains value-redacted.  This runner-owned receipt is
    carried only as executable oracle data so post-change verification can recreate
    the researched scenario instead of silently dropping the controlled inputs.
    """

    return content_bound_payload(
        {
            "schema_version": 1,
            "source_experiment_id": source_experiment_id,
            "environment": {
                key: value for key, value in sorted(environment_overrides.items())
            },
            "disposable_state_paths": list(disposable_state_paths),
            "runner_approved": True,
        },
        hash_field="replay_inputs_sha256",
    )


def _replay_inputs_attested(value: Any, *, experiment_id: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    projection = {key: item for key, item in value.items() if key != "replay_inputs_sha256"}
    environment = value.get("environment")
    paths = value.get("disposable_state_paths")
    if (
        value.get("schema_version") != 1
        or value.get("source_experiment_id") != experiment_id
        or value.get("runner_approved") is not True
        or value.get("replay_inputs_sha256") != _canonical_json_sha256(projection)
        or not isinstance(environment, Mapping)
        or not isinstance(paths, list)
    ):
        return False
    if any(
        not isinstance(key, str)
        or _REPLAY_ENVIRONMENT_KEY_RE.fullmatch(key) is None
        or _SENSITIVE_ENVIRONMENT_RE.search(key) is not None
        or key.upper() in _REPLAY_FORBIDDEN_ENVIRONMENT_KEYS
        or key.upper().startswith(_REPLAY_FORBIDDEN_ENVIRONMENT_PREFIXES)
        or (item is not None and not isinstance(item, str))
        for key, item in environment.items()
    ):
        return False
    return all(
        isinstance(path, str)
        and bool(path)
        and not PurePosixPath(path.replace("\\", "/")).is_absolute()
        and not PureWindowsPath(path).anchor
        and ".." not in PurePosixPath(path.replace("\\", "/")).parts
        and ".." not in PureWindowsPath(path).parts
        for path in paths
    )


@dataclass(frozen=True)
class ReplayExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    execution_metadata: dict[str, Any]


class ReplayExecutor(Protocol):
    """Explicit execution boundary for model-authored replay commands."""

    def isolation_receipt(self, *, source_workspace: Path) -> dict[str, Any]: ...

    def execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        source_workspace: Path,
        timeout_seconds: float | None,
        environment_overrides: Mapping[str, str | None] | None = None,
    ) -> ReplayExecutionResult: ...


@dataclass(frozen=True)
class PlatformRoutingReplayExecutor:
    """Select an explicitly approved executor for each declared platform.

    The router makes Windows-only and other platform-specific failures operable
    without pretending a Linux container reproduced them.  Each route keeps its own
    isolation receipt; unsupported platforms fail closed for that experiment only.
    """

    default_executor: ReplayExecutor
    platform_executors: Mapping[str, ReplayExecutor]

    def executor_for_platform(self, requirement: str) -> ReplayExecutor:
        if requirement == "any":
            return self.default_executor
        return self.platform_executors.get(
            requirement.casefold(),
            BlockedReplayExecutor(reason=f"platform_route_unavailable:{requirement}"),
        )

    def isolation_receipt(self, *, source_workspace: Path) -> dict[str, Any]:
        routes = {
            requirement: executor.isolation_receipt(source_workspace=source_workspace)
            for requirement, executor in sorted(self.platform_executors.items())
        }
        default = self.default_executor.isolation_receipt(source_workspace=source_workspace)
        route_receipts = [default, *routes.values()]
        trusted = all(receipt.get("trust_decision") != "denied" for receipt in route_receipts)
        return {
            "executor": "platform_router",
            "platform": "routed",
            "os_sandbox": None,
            "network": "per_route",
            "filesystem_isolation": "per_route",
            "trust_decision": "explicit_routes" if trusted else "denied",
            "trust_reason": "repo_configured_platform_routes",
            "source_workspace": str(source_workspace.resolve()),
            "sanitized_environment_keys": sorted(
                {
                    key
                    for receipt in route_receipts
                    for key in receipt.get("sanitized_environment_keys", [])
                    if isinstance(key, str)
                }
            ),
            "default": default,
            "routes": routes,
        }

    def execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        source_workspace: Path,
        timeout_seconds: float | None,
        environment_overrides: Mapping[str, str | None] | None = None,
    ) -> ReplayExecutionResult:
        del argv, cwd, source_workspace, timeout_seconds, environment_overrides
        raise RuntimeError("platform_router_requires_declared_platform_selection")


@dataclass(frozen=True)
class BlockedReplayExecutor:
    reason: str = "no_explicit_replay_executor"

    def isolation_receipt(self, *, source_workspace: Path) -> dict[str, Any]:
        return {
            "executor": "blocked",
            "platform": "unavailable",
            "os_sandbox": False,
            "network": "unavailable",
            "filesystem_isolation": "unavailable",
            "trust_decision": "denied",
            "trust_reason": self.reason,
            "source_workspace": str(source_workspace.resolve()),
            "sanitized_environment_keys": [],
        }

    def execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        source_workspace: Path,
        timeout_seconds: float | None,
        environment_overrides: Mapping[str, str | None] | None = None,
    ) -> ReplayExecutionResult:
        del argv, cwd, source_workspace, timeout_seconds, environment_overrides
        raise RuntimeError(self.reason)


@dataclass(frozen=True)
class TrustedHostReplayExecutor:
    """Unsandboxed host replay, allowed only for explicitly approved source roots."""

    approved_source_roots: Sequence[Path]
    source_identity: Path | None = None

    def _approved_root(self, source_workspace: Path) -> Path | None:
        source = (self.source_identity or source_workspace).resolve()
        for root in self.approved_source_roots:
            approved = root.resolve()
            if source == approved or _within(source, approved):
                return approved
        return None

    def isolation_receipt(self, *, source_workspace: Path) -> dict[str, Any]:
        approved = self._approved_root(source_workspace)
        environment = _sanitized_replay_environment() if approved is not None else {}
        return {
            "executor": "trusted_host",
            "platform": platform.system().casefold(),
            "os_sandbox": False,
            "network": "not_enforced",
            "filesystem_isolation": "dedicated_clone_only_not_os_sandbox",
            "trust_decision": ("approved_local_source_root" if approved is not None else "denied"),
            "trust_reason": (
                str(approved) if approved is not None else "source_outside_approved_roots"
            ),
            "source_workspace": str(source_workspace.resolve()),
            "trusted_source_identity": str((self.source_identity or source_workspace).resolve()),
            "sanitized_environment_keys": sorted(environment),
        }

    def execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        source_workspace: Path,
        timeout_seconds: float | None,
        environment_overrides: Mapping[str, str | None] | None = None,
    ) -> ReplayExecutionResult:
        if self._approved_root(source_workspace) is None:
            raise PermissionError("source_outside_approved_roots")
        environment = _sanitized_replay_environment()
        absent_keys: list[str] = []
        for key, value in (environment_overrides or {}).items():
            if value is None:
                environment.pop(key, None)
                absent_keys.append(key)
            else:
                environment[key] = value
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
            env=environment,
        )
        return ReplayExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            execution_metadata={
                "executor": "trusted_host",
                "os_sandbox": False,
                "network": "not_enforced",
                "environment_attestation": environment_attestation(
                    environment,
                    absent_keys=absent_keys,
                ),
            },
        )


@dataclass(frozen=True)
class DockerReplayExecutor:
    """Replay inside an explicitly selected Docker image with networking disabled."""

    image_ref: str

    def isolation_receipt(self, *, source_workspace: Path) -> dict[str, Any]:
        image = self.image_ref.strip()
        return {
            "executor": "docker",
            # Usertest's maintenance image is a Linux container.  Record this
            # explicitly so a Windows-only failure cannot be "proved" by a
            # replay that merely happened inside Docker Desktop on Windows.
            "platform": "linux",
            "os_sandbox": bool(image),
            "network": "none" if image else "unavailable",
            "filesystem_isolation": "dedicated_clone_bind_mount",
            "trust_decision": "explicit_image" if image else "denied",
            "trust_reason": image or "docker_image_missing",
            "source_workspace": str(source_workspace.resolve()),
            "sanitized_environment_keys": ["CI"],
        }

    def execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        source_workspace: Path,
        timeout_seconds: float | None,
        environment_overrides: Mapping[str, str | None] | None = None,
    ) -> ReplayExecutionResult:
        del source_workspace
        image = self.image_ref.strip()
        if not image:
            raise PermissionError("docker_image_missing")
        sandbox_artifacts = cwd.parent / f".{cwd.name}.docker_replay"
        overrides = dict(environment_overrides or {})
        applied_environment = {
            "CI": "1",
            **{key: value for key, value in overrides.items() if value is not None},
        }
        absent_keys = [key for key, value in overrides.items() if value is None]
        spec = SandboxSpec(
            backend="docker",
            image_ref=image,
            network_mode="none",
            env_overrides=applied_environment,
            docker_timeout_seconds=timeout_seconds,
        )
        instance = DockerSandbox(cwd, sandbox_artifacts, spec).start()
        sandbox_meta_path = sandbox_artifacts / "sandbox.json"
        sandbox_meta_sha256 = (
            _sha256_path(sandbox_meta_path) if sandbox_meta_path.is_file() else None
        )
        sandbox_meta: dict[str, Any] = {}
        if sandbox_meta_path.is_file():
            try:
                loaded = json.loads(sandbox_meta_path.read_text(encoding="utf-8"))
                sandbox_meta = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                sandbox_meta = {}
        image_id: str | None = None
        try:
            image_inspect = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", instance.image_tag],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
            )
            if image_inspect.returncode == 0 and image_inspect.stdout.strip():
                image_id = image_inspect.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            image_id = None
        try:
            completed = subprocess.run(
                [
                    *instance.command_prefix,
                    *(
                        ["env", *[part for key in absent_keys for part in ("-u", key)]]
                        if absent_keys
                        else []
                    ),
                    *argv,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
            )
        finally:
            instance.close()
        cleanup_confirmed = False
        try:
            inspect = subprocess.run(
                ["docker", "inspect", instance.container_name],
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            cleanup_confirmed = inspect.returncode != 0
        except (OSError, subprocess.SubprocessError):
            cleanup_confirmed = False
        return ReplayExecutionResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            execution_metadata={
                "executor": "docker",
                "sandbox_metadata_path": str(sandbox_meta_path),
                "sandbox_metadata_sha256": sandbox_meta_sha256,
                "backend": sandbox_meta.get("backend"),
                "image_tag": sandbox_meta.get("image_tag"),
                "image_hash": sandbox_meta.get("image_hash"),
                "image_id": image_id,
                "network": sandbox_meta.get("network_mode"),
                "container_name": instance.container_name,
                "cleanup_attempted": True,
                "cleanup_confirmed": cleanup_confirmed,
                "environment_attestation": environment_attestation(
                    applied_environment,
                    absent_keys=absent_keys,
                ),
            },
        )


def _isolation_receipt_errors(receipt: Any) -> list[str]:
    if not isinstance(receipt, dict):
        return ["replay_isolation_receipt_invalid"]
    executor = receipt.get("executor")
    trust_decision = receipt.get("trust_decision")
    errors: list[str] = []
    if executor == "platform_router":
        default = receipt.get("default")
        routes = receipt.get("routes")
        if (
            trust_decision != "explicit_routes"
            or not isinstance(default, dict)
            or not isinstance(routes, dict)
            or not routes
        ):
            errors.append("replay_platform_router_invalid")
        else:
            for route_receipt in [default, *routes.values()]:
                route_errors = _isolation_receipt_errors(route_receipt)
                errors.extend(f"replay_platform_route:{error}" for error in route_errors)
    elif executor == "docker":
        if (
            receipt.get("os_sandbox") is not True
            or receipt.get("network") != "none"
            or trust_decision != "explicit_image"
        ):
            errors.append("replay_docker_isolation_invalid")
    elif executor == "trusted_host":
        if (
            receipt.get("os_sandbox") is not False
            or receipt.get("network") != "not_enforced"
            or trust_decision != "approved_local_source_root"
        ):
            errors.append("replay_host_trust_invalid")
    else:
        errors.append("replay_executor_untrusted")
    keys = receipt.get("sanitized_environment_keys")
    if not isinstance(keys, list) or any(
        not isinstance(key, str) or _SENSITIVE_ENVIRONMENT_RE.search(key) for key in keys
    ):
        errors.append("replay_environment_attestation_invalid")
    if _text(receipt.get("source_workspace")) is None:
        errors.append("replay_source_workspace_missing")
    return errors


def _execution_metadata_errors(
    metadata: Any,
    *,
    isolation: dict[str, Any],
) -> list[str]:
    if not isinstance(metadata, dict):
        return ["replay_execution_metadata_invalid"]
    if metadata.get("executor") != isolation.get("executor"):
        return ["replay_execution_executor_mismatch"]
    if metadata.get("executor") == "trusted_host":
        if metadata.get("os_sandbox") is not False or metadata.get("network") != "not_enforced":
            return ["replay_host_execution_metadata_invalid"]
        return []
    if metadata.get("executor") != "docker":
        return ["replay_execution_executor_untrusted"]
    errors: list[str] = []
    sandbox_path_raw = _text(metadata.get("sandbox_metadata_path"))
    sandbox_path = Path(sandbox_path_raw) if sandbox_path_raw is not None else None
    if (
        sandbox_path is None
        or not sandbox_path.is_file()
        or metadata.get("sandbox_metadata_sha256") != _sha256_path(sandbox_path)
    ):
        errors.append("replay_sandbox_metadata_unverifiable")
    else:
        try:
            raw = json.loads(sandbox_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if (
            not isinstance(raw, dict)
            or raw.get("backend") != "docker"
            or raw.get("network_mode") != "none"
            or raw.get("container_name") != metadata.get("container_name")
            or raw.get("image_tag") != metadata.get("image_tag")
        ):
            errors.append("replay_sandbox_metadata_mismatch")
    image_id = metadata.get("image_id")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        errors.append("replay_docker_image_digest_missing")
    if metadata.get("network") != "none" or metadata.get("backend") != "docker":
        errors.append("replay_docker_network_unverified")
    if (
        metadata.get("cleanup_attempted") is not True
        or metadata.get("cleanup_confirmed") is not True
    ):
        errors.append("replay_docker_cleanup_unconfirmed")
    return errors


def _copy_attested_overlay(
    *,
    source_workspace: Path,
    replay_workspace: Path,
    overlay_manifest: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for relative, expected in sorted(overlay_manifest.items()):
        if not relative.startswith(".usertest_research/") or not isinstance(expected, dict):
            errors.append(f"overlay_entry_invalid:{relative}")
            continue
        source = (source_workspace / relative).resolve()
        destination = (replay_workspace / relative).resolve()
        if (
            not _within(source, source_workspace.resolve())
            or not _within(destination, replay_workspace.resolve())
            or expected.get("kind") != "file"
            or not source.is_file()
            or source.is_symlink()
        ):
            errors.append(f"overlay_entry_unsafe:{relative}")
            continue
        if expected.get("sha256") != _sha256_path(source):
            errors.append(f"overlay_entry_hash_changed:{relative}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied = _workspace_manifest(replay_workspace).get(relative)
        if copied != expected:
            errors.append(f"overlay_entry_copy_mismatch:{relative}")
    return errors


def _assert_observable(
    assertion: dict[str, Any],
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> bool:
    source = assertion.get("source")
    operator = assertion.get("operator")
    expected = assertion.get("expected")
    if source == "exit_code":
        return operator == "equals" and exit_code == expected
    observed = {
        "stdout": stdout,
        "stderr": stderr,
        "combined": stdout + stderr,
    }.get(str(source))
    if observed is None or not isinstance(expected, str):
        return False
    if operator == "equals":
        return observed.strip() == expected.strip()
    if operator == "contains":
        return expected in observed
    if operator == "not_contains":
        return expected not in observed
    return False


def _clean_replay_receipts(
    dossier: dict[str, Any],
    *,
    evidence_assignment: dict[str, Any] | None = None,
    baseline_workspace: Path,
    research_workspace: Path,
    overlay_manifest: dict[str, Any],
    replay_root: Path,
    repo_revision: str,
    timeout_seconds: float | None,
    errors: list[str],
    replay_executor: ReplayExecutor | None = None,
) -> dict[str, dict[str, Any]]:
    """Replay every declared experiment in an independent clean baseline clone."""
    executor: ReplayExecutor = replay_executor or BlockedReplayExecutor()
    router_isolation = executor.isolation_receipt(source_workspace=research_workspace)
    if router_isolation.get("trust_decision") == "denied":
        errors.append(
            "replay_isolation_unavailable:"
            f"{router_isolation.get('trust_reason') or 'untrusted_executor'}"
        )
        return {}
    experiments_raw = dossier.get("experiments")
    experiments = experiments_raw if isinstance(experiments_raw, list) else []
    replay_root.mkdir(parents=True, exist_ok=True)
    receipts: dict[str, dict[str, Any]] = {}
    signatures: dict[tuple[Any, ...], tuple[str, str]] = {}
    artifact_refs_raw = dossier.get("artifact_refs")
    artifact_refs = artifact_refs_raw if isinstance(artifact_refs_raw, list) else []
    assignment = evidence_assignment if isinstance(evidence_assignment, dict) else {}

    for index, experiment in enumerate(experiments):
        if not isinstance(experiment, dict):
            continue
        experiment_id = _text(experiment.get("experiment_id"))
        command = _text(experiment.get("command"))
        if experiment_id is None or command is None:
            continue
        setup_error_count = len(errors)
        environment_overrides = _replay_environment_overrides(
            experiment,
            experiment_id=experiment_id,
            errors=errors,
        )
        if len(errors) != setup_error_count:
            continue
        authorized = _authorized_replay_invocation(
            command=command,
            experiment=experiment,
            dossier=dossier,
            assignment=assignment,
            workspace=research_workspace,
        )
        if authorized is None:
            errors.append(f"experiment_command_not_authorized:{experiment_id}")
            continue
        replay_argv, command_authorization = authorized
        required_platform = _text(experiment.get("platform_requirement")) or "any"
        selected_executor = (
            executor.executor_for_platform(required_platform)
            if isinstance(executor, PlatformRoutingReplayExecutor)
            else executor
        )
        isolation = selected_executor.isolation_receipt(source_workspace=research_workspace)
        if isolation.get("trust_decision") == "denied":
            errors.append(
                f"experiment_platform_route_unavailable:{experiment_id}:{required_platform}"
            )
            continue
        actual_platform = _text(isolation.get("platform")) or "unknown"
        if (
            required_platform != "any"
            and required_platform.casefold() != actual_platform.casefold()
        ):
            errors.append(
                f"experiment_platform_mismatch:{experiment_id}:"
                f"required={required_platform}:actual={actual_platform}"
            )
            continue
        replay_id = sha256(f"{index}:{experiment_id}:{command}".encode()).hexdigest()[:16]
        workspace = replay_root / f"workspace_{replay_id}"
        acquired, head, clean, acquire_errors = materialize_clean_revision_view(
            source_workspace=baseline_workspace,
            destination=workspace,
            repo_revision=repo_revision,
        )
        if acquire_errors or acquired is None:
            errors.extend(
                f"experiment_replay_workspace:{experiment_id}:{error}" for error in acquire_errors
            )
            continue
        if clean is not True or head != repo_revision:
            errors.append(f"experiment_replay_workspace_invalid:{experiment_id}")
            continue
        baseline_state = _canonical_workspace_state(acquired)
        overlay_errors = _copy_attested_overlay(
            source_workspace=research_workspace,
            replay_workspace=acquired,
            overlay_manifest=overlay_manifest,
        )
        if overlay_errors:
            errors.extend(
                f"experiment_replay_overlay:{experiment_id}:{error}" for error in overlay_errors
            )
            continue
        setup_error_count = len(errors)
        disposable_state_paths = _declared_disposable_state_paths(
            experiment,
            workspace=acquired,
            experiment_id=experiment_id,
            errors=errors,
        )
        if len(errors) != setup_error_count:
            continue
        replay_setup_receipt = _replay_setup_receipt(
            environment_overrides=environment_overrides,
            disposable_state_paths=disposable_state_paths,
        )
        replay_inputs = _replay_inputs_receipt(
            source_experiment_id=experiment_id,
            environment_overrides=environment_overrides,
            disposable_state_paths=disposable_state_paths,
        )
        pre_replay_state = _canonical_workspace_state(acquired)
        try:
            completed = selected_executor.execute(
                replay_argv,
                cwd=acquired,
                source_workspace=research_workspace,
                timeout_seconds=timeout_seconds,
                environment_overrides=environment_overrides,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"experiment_replay_failed:{experiment_id}:{type(exc).__name__}")
            continue
        except (PermissionError, RuntimeError) as exc:
            errors.append(f"experiment_replay_isolation_failed:{experiment_id}:{exc}")
            continue
        post_replay_state = _canonical_workspace_state(acquired)
        post_replay_mutations = pre_replay_state != post_replay_state
        undeclared_mutations, state_transition_receipts = (
            _declared_state_transition_receipts(
                before=pre_replay_state,
                after=post_replay_state,
                declared=disposable_state_paths,
            )
        )
        index_or_head_changed = any(
            pre_replay_state.get(field) != post_replay_state.get(field)
            for field in (
                "head",
                "git_index_stage_sha256",
                "git_index_flags_sha256",
            )
        )
        if undeclared_mutations or index_or_head_changed:
            errors.append(f"experiment_replay_workspace_mutated:{experiment_id}")
        metadata_errors = _execution_metadata_errors(
            completed.execution_metadata,
            isolation=isolation,
        )
        if environment_overrides and replay_environment_attestation(
            {"execution_metadata": completed.execution_metadata}
        ) is None:
            metadata_errors.append("replay_environment_attestation_unverifiable")
        errors.extend(
            f"experiment_replay_isolation:{experiment_id}:{error}" for error in metadata_errors
        )
        stdout = completed.stdout
        stderr = completed.stderr
        evidence_dir = replay_root / f"evidence_{replay_id}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = evidence_dir / "stdout.txt"
        stderr_path = evidence_dir / "stderr.txt"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        stdout_id = f"runner:replay:{experiment_id}:stdout"
        stderr_id = f"runner:replay:{experiment_id}:stderr"
        artifact_refs.extend(
            [
                {
                    "artifact_id": stdout_id,
                    "kind": "replay_stdout",
                    "path": str(stdout_path),
                    "description": "Runner-owned clean replay stdout",
                },
                {
                    "artifact_id": stderr_id,
                    "kind": "replay_stderr",
                    "path": str(stderr_path),
                    "description": "Runner-owned clean replay stderr",
                },
            ]
        )
        experiment_artifacts_raw = experiment.get("artifact_refs")
        experiment_artifacts = (
            list(experiment_artifacts_raw) if isinstance(experiment_artifacts_raw, list) else []
        )
        experiment_artifacts.extend([stdout_id, stderr_id])
        experiment["artifact_refs"] = list(dict.fromkeys(experiment_artifacts))

        assertion_raw = experiment.get("observable_assertion")
        assertion = assertion_raw if isinstance(assertion_raw, dict) else {}
        assertion_passed = _assert_observable(
            assertion,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        declared_exit = experiment.get("exit_code")
        if declared_exit != completed.returncode:
            errors.append(
                f"experiment_replay_exit_mismatch:{experiment_id}:"
                f"{declared_exit}:{completed.returncode}"
            )
        if not assertion_passed:
            errors.append(f"experiment_observable_assertion_failed:{experiment_id}")
        receipt = {
            "experiment_id": experiment_id,
            "scenario_kind": experiment.get("scenario_kind"),
            "addresses_atom_ids": experiment.get("addresses_atom_ids"),
            "command": command,
            "executed_argv": replay_argv,
            "command_authorization": command_authorization,
            "declared_result": experiment.get("result"),
            "outcome": experiment.get("outcome"),
            "exit_code": completed.returncode,
            "workspace_dir": str(acquired),
            "workspace_head": head,
            "baseline_state_sha256": _canonical_json_sha256(baseline_state),
            "pre_replay_state_sha256": _canonical_json_sha256(pre_replay_state),
            "post_replay_state_sha256": _canonical_json_sha256(post_replay_state),
            "post_replay_mutations": post_replay_mutations,
            "undeclared_post_replay_mutations": undeclared_mutations,
            "declared_state_transitions": state_transition_receipts,
            "replay_setup_receipt": replay_setup_receipt,
            "replay_inputs": replay_inputs,
            "overlay_manifest_sha256": _canonical_json_sha256(overlay_manifest),
            "execution_isolation": isolation,
            "execution_metadata": completed.execution_metadata,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_sha256": _sha256_path(stdout_path),
            "stderr_sha256": _sha256_path(stderr_path),
            "observable_assertion": assertion,
            "verification_boundary": experiment.get("verification_boundary"),
            "assertion_passed": assertion_passed,
            "artifact_refs": list(experiment["artifact_refs"]),
        }
        receipts[experiment_id] = receipt
        signature = (
            command,
            completed.returncode,
            receipt["stdout_sha256"],
            receipt["stderr_sha256"],
        )
        previous = signatures.get(signature)
        outcome = str(experiment.get("outcome") or "")
        if previous is not None and previous[1] != outcome:
            errors.append(f"contradictory_experiment_outcomes:{previous[0]}:{experiment_id}")
        signatures[signature] = (experiment_id, outcome)
    dossier["artifact_refs"] = artifact_refs
    return receipts


def _causal_trace_match(
    *,
    output: str,
    relative_path: str,
    symbol: str,
) -> tuple[str, str] | None:
    """Find a runner output frame that binds an executed replay to one symbol."""
    normalized = output.replace("\\", "/")
    path_pattern = re.escape(relative_path.replace("\\", "/"))
    leaf = symbol.rsplit(".", 1)[-1]
    leaf_pattern = re.escape(leaf)
    patterns = (
        (
            "python_traceback",
            rf"File\s+[\"'][^\"']*{path_pattern}[\"']\s*,\s*line\s+\d+"
            rf"\s*,\s*in\s+{leaf_pattern}\b",
        ),
        (
            "pytest_traceback",
            rf"(?m)^\s*[^\n]*{path_pattern}:\d+:\s+in\s+{leaf_pattern}\b",
        ),
        (
            "node_stack",
            rf"(?m)^\s*at\s+(?:[^\n.(]+\.)*{leaf_pattern}\s*"
            rf"\([^\n)]*{path_pattern}:\d+:\d+\)",
        ),
        (
            "python_import_error",
            rf"ImportError:[^\n]*cannot import name\s+[\"']?{leaf_pattern}[\"']?"
            rf"[^\n]*\([^\n)]*{path_pattern}\)",
        ),
    )
    for kind, pattern in patterns:
        match = re.search(pattern, normalized)
        if match is not None:
            return kind, match.group(0)
    return None


def _causal_link_receipts(
    dossier: dict[str, Any],
    *,
    clean_replays: dict[str, dict[str, Any]],
    symbol_receipts: list[dict[str, str]],
    errors: list[str],
) -> list[dict[str, Any]]:
    symbol_paths = {
        str(receipt.get("symbol")): str(receipt.get("path"))
        for receipt in symbol_receipts
        if _text(receipt.get("symbol")) is not None and _text(receipt.get("path")) is not None
    }
    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    links: list[dict[str, Any]] = []
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = _text(hypothesis.get("hypothesis_id"))
        support_raw = hypothesis.get("supporting_evidence")
        support_ids = support_raw if isinstance(support_raw, list) else []
        symbols_raw = hypothesis.get("mechanism_symbols")
        symbols = symbols_raw if isinstance(symbols_raw, list) else []
        for raw_symbol in symbols:
            symbol = _text(raw_symbol)
            relative_path = symbol_paths.get(symbol or "")
            if hypothesis_id is None or symbol is None or relative_path is None:
                continue
            causal_link: dict[str, Any] | None = None
            for raw_experiment_id in support_ids:
                experiment_id = _text(raw_experiment_id)
                experiment = experiments.get(experiment_id or "")
                replay = clean_replays.get(experiment_id or "")
                if (
                    experiment_id is None
                    or not isinstance(experiment, dict)
                    or not isinstance(replay, dict)
                    or experiment.get("outcome") != "supports"
                    or experiment.get("scenario_kind") not in {"original_replay", "faithful_replay"}
                ):
                    continue
                if replay_invocation_references_model_overlay(
                    experiment.get("command"),
                    replay.get("executed_argv"),
                ):
                    errors.append(
                        "mechanism_causal_trace_model_overlay_untrusted:"
                        f"{hypothesis_id}:{experiment_id}:{symbol}"
                    )
                    continue
                for stream in ("stderr", "stdout"):
                    output_path_raw = _text(replay.get(f"{stream}_path"))
                    output_path = Path(output_path_raw) if output_path_raw is not None else None
                    if output_path is None or not output_path.is_file():
                        continue
                    output = output_path.read_text(encoding="utf-8", errors="replace")
                    match = _causal_trace_match(
                        output=output,
                        relative_path=relative_path,
                        symbol=symbol,
                    )
                    if match is None:
                        continue
                    kind, excerpt = match
                    causal_link = {
                        "hypothesis_id": hypothesis_id,
                        "experiment_id": experiment_id,
                        "symbol": symbol,
                        "path": relative_path,
                        "stream": stream,
                        "trace_kind": kind,
                        "trace_excerpt_sha256": sha256(excerpt.encode("utf-8")).hexdigest(),
                        "stream_sha256": _sha256_path(output_path),
                    }
                    break
                if causal_link is not None:
                    break
            if causal_link is None:
                errors.append(f"mechanism_causal_trace_missing:{hypothesis_id}:{symbol}")
            else:
                links.append(causal_link)
    return links


def _load_events(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    if not path.exists() or not path.is_file():
        errors.append("normalized_events_missing")
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    errors.append(f"normalized_event_not_object:{line_number}")
                    continue
                events.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"normalized_events_unreadable:{type(exc).__name__}")
    return events


def _resolve_evidence_path(
    raw_path: str,
    *,
    run_dir: Path,
    workspace: Path | None,
) -> Path | None:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        roots = [run_dir.resolve(), *([workspace] if workspace is not None else [])]
        return resolved if any(_within(resolved, root) for root in roots) else None

    roots = [*([workspace] if workspace is not None else []), run_dir.resolve()]
    for root in roots:
        resolved = (root / candidate).resolve()
        if _within(resolved, root) and resolved.exists():
            return resolved
    return None


def _artifact_receipts(
    dossier: dict[str, Any],
    *,
    run_dir: Path,
    workspace: Path | None,
    errors: list[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    refs = dossier.get("artifact_refs")
    if not isinstance(refs, list):
        errors.append("artifact_refs_not_list")
        return [], set()

    receipts: list[dict[str, Any]] = []
    reference_keys: set[str] = set()
    seen_ids: set[str] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            errors.append(f"artifact_ref_not_object:{index}")
            continue
        artifact_id = _text(ref.get("artifact_id"))
        raw_path = _text(ref.get("path"))
        kind = _text(ref.get("kind"))
        if artifact_id is None or raw_path is None or kind is None:
            errors.append(f"artifact_ref_identity_missing:{index}")
            continue
        if artifact_id in seen_ids:
            errors.append(f"artifact_id_duplicate:{artifact_id}")
            continue
        seen_ids.add(artifact_id)
        resolved = _resolve_evidence_path(raw_path, run_dir=run_dir, workspace=workspace)
        if resolved is None:
            errors.append(f"artifact_unresolved:{artifact_id}")
            continue
        if not resolved.is_file():
            errors.append(f"artifact_not_file:{artifact_id}")
            continue
        digest = _sha256_path(resolved)
        receipts.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "declared_path": raw_path,
                "path": str(resolved),
                "sha256": digest,
                "size_bytes": resolved.stat().st_size,
            }
        )
        reference_keys.update({artifact_id, raw_path, str(resolved)})
    return receipts, reference_keys


def _experiment_receipts(
    dossier: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    artifact_keys: set[str],
    clean_replays: dict[str, dict[str, Any]],
    errors: list[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    command_events: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for event_index, event in enumerate(events):
        data = event.get("data")
        if event.get("type") == "run_command" and isinstance(data, dict):
            command_events.append((event_index, event, data))

    receipts: list[dict[str, Any]] = []
    outcomes: dict[str, str] = {}
    experiments = dossier.get("experiments")
    if not isinstance(experiments, list):
        errors.append("experiments_not_list")
        return receipts, outcomes

    seen_ids: set[str] = set()
    used_event_indexes: set[int] = set()
    for index, experiment in enumerate(experiments):
        if not isinstance(experiment, dict):
            errors.append(f"experiment_not_object:{index}")
            continue
        experiment_id = _text(experiment.get("experiment_id"))
        command = _text(experiment.get("command"))
        exit_code = experiment.get("exit_code")
        outcome = _text(experiment.get("outcome"))
        if experiment_id is None or command is None:
            errors.append(f"experiment_identity_missing:{index}")
            continue
        if experiment_id in seen_ids:
            errors.append(f"experiment_id_duplicate:{experiment_id}")
            continue
        seen_ids.add(experiment_id)
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            errors.append(f"experiment_exit_code_missing:{experiment_id}")
            continue

        normalized = _normalize_command(command)
        matches = [
            (event_index, event, data)
            for event_index, event, data in command_events
            if _normalize_command(str(data.get("command") or "")) == normalized
            and data.get("exit_code") == exit_code
            and event_index not in used_event_indexes
        ]
        if not matches:
            errors.append(f"experiment_command_not_observed:{experiment_id}")
            continue

        artifact_refs = experiment.get("artifact_refs")
        if not isinstance(artifact_refs, list) or not artifact_refs:
            errors.append(f"experiment_artifact_refs_missing:{experiment_id}")
            continue
        unresolved_refs = [
            str(ref)
            for ref in artifact_refs
            if not isinstance(ref, str) or ref.strip() not in artifact_keys
        ]
        if unresolved_refs:
            errors.append(
                f"experiment_artifact_refs_unresolved:{experiment_id}:" + ",".join(unresolved_refs)
            )
            continue

        event_index, event, data = matches[0]
        used_event_indexes.add(event_index)
        clean_replay = clean_replays.get(experiment_id)
        if clean_replay is None:
            errors.append(f"experiment_clean_replay_missing:{experiment_id}")
            continue
        receipts.append(
            {
                **clean_replay,
                "agent_event_index": event_index,
                "agent_event_sha256": _canonical_json_sha256(event),
                "agent_output_excerpt_sha256": (
                    sha256(str(data["output_excerpt"]).encode()).hexdigest()
                    if _text(data.get("output_excerpt")) is not None
                    else None
                ),
            }
        )
        if outcome is not None:
            outcomes[experiment_id] = outcome
    return receipts, outcomes


def _workspace_file(
    raw_path: str,
    *,
    workspace: Path,
) -> Path | None:
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    return resolved if _within(resolved, workspace) else None


def _inspection_receipts(
    dossier: dict[str, Any],
    *,
    workspace: Path | None,
    events: list[dict[str, Any]],
    errors: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if workspace is None:
        return [], []
    read_paths: dict[str, list[dict[str, Any]]] = {}
    for event_index, event in enumerate(events):
        data = event.get("data")
        if (
            event.get("type") != "read_file"
            or not isinstance(data, dict)
            or _text(data.get("path")) is None
        ):
            continue
        read_source = _text(data.get("read_source"))
        source_exit_code = data.get("source_exit_code")
        bytes_read = data.get("bytes")
        observed_content = data.get("observed_content")
        observed_hash = data.get("observed_content_sha256")
        observed_start = data.get("observed_start_line")
        observed_end = data.get("observed_end_line")
        observed_bytes = data.get("observed_bytes")
        if (
            data.get("content_observed") is not True
            or read_source not in {"tool", "shell_command"}
            or source_exit_code != 0
            or isinstance(bytes_read, bool)
            or not isinstance(bytes_read, int)
            or bytes_read < 0
            or not isinstance(observed_content, str)
            or observed_hash != sha256(observed_content.encode("utf-8")).hexdigest()
            or isinstance(observed_bytes, bool)
            or not isinstance(observed_bytes, int)
            or observed_bytes != len(observed_content.encode("utf-8"))
            or isinstance(observed_start, bool)
            or not isinstance(observed_start, int)
            or isinstance(observed_end, bool)
            or not isinstance(observed_end, int)
            or observed_start < 1
            or observed_end < observed_start
        ):
            continue
        read_paths.setdefault(_normalize_path(str(data.get("path"))), []).append(
            {
                "event_index": event_index,
                "event_sha256": _canonical_json_sha256(event),
                "read_source": read_source,
                "file_size_bytes": bytes_read,
                "file_sha256": data.get("file_sha256"),
                "whole_file_observed": data.get("whole_file_observed") is True,
                "observed_content": observed_content,
                "observed_content_sha256": observed_hash,
                "observed_bytes": observed_bytes,
                "observed_start_line": observed_start,
                "observed_end_line": observed_end,
            }
        )

    file_receipts: list[dict[str, Any]] = []
    file_texts: list[tuple[str, str]] = []
    inspected_files = dossier.get("inspected_files")
    if not isinstance(inspected_files, list):
        errors.append("inspected_files_not_list")
        return file_receipts, []
    for raw in inspected_files:
        raw_path = _text(raw)
        if raw_path is None:
            continue
        path = _workspace_file(raw_path, workspace=workspace)
        if path is None or not path.is_file():
            errors.append(f"inspected_file_unresolved:{raw_path}")
            continue
        relative = path.relative_to(workspace).as_posix()
        normalized_relative = _normalize_path(relative)
        read_candidates = read_paths.get(normalized_relative, [])
        read_receipt = next(
            (
                candidate
                for candidate in sorted(
                    read_candidates,
                    key=lambda item: (bool(item["whole_file_observed"]), item["event_index"]),
                    reverse=True,
                )
                if candidate.get("file_sha256") == _sha256_path(path)
                and candidate.get("file_size_bytes") == path.stat().st_size
            ),
            None,
        )
        if read_receipt is None:
            errors.append(f"inspected_file_not_observed:{raw_path}")
            continue
        observed_content = str(read_receipt["observed_content"])
        file_texts.append((relative, observed_content))
        git_blob = _git_blob_sha(workspace, relative)
        if git_blob is None:
            errors.append(f"inspected_file_not_in_baseline_revision:{raw_path}")
            continue
        file_receipts.append(
            {
                "path": relative,
                "sha256": _sha256_path(path),
                "git_blob_sha": git_blob,
                "size_bytes": path.stat().st_size,
                "read_event_index": read_receipt["event_index"],
                "read_event_sha256": read_receipt["event_sha256"],
                "read_source": read_receipt["read_source"],
                "bytes_observed": read_receipt["observed_bytes"],
                "whole_file_observed": read_receipt["whole_file_observed"],
                "observed_content_sha256": read_receipt["observed_content_sha256"],
                "observed_start_line": read_receipt["observed_start_line"],
                "observed_end_line": read_receipt["observed_end_line"],
            }
        )

    symbol_receipts: list[dict[str, str]] = []
    inspected_symbols = dossier.get("inspected_symbols")
    if not isinstance(inspected_symbols, list):
        errors.append("inspected_symbols_not_list")
        return file_receipts, symbol_receipts
    for raw in inspected_symbols:
        symbol = _text(raw)
        if symbol is None:
            continue
        matches = [
            path
            for path, content in file_texts
            if _symbol_definition_exists(path=path, content=content, symbol=symbol)
        ]
        if not matches:
            errors.append(f"inspected_symbol_unresolved:{symbol}")
            continue
        symbol_receipts.append({"symbol": symbol, "path": matches[0]})
    return file_receipts, symbol_receipts


def _git_blob_sha(workspace: Path, relative_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", f"HEAD:{relative_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _python_definitions(content: str) -> set[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    definitions: set[str] = set()

    def add_binding(name: str, prefix: tuple[str, ...]) -> None:
        definitions.add(".".join((*prefix, name)))

    def visit(nodes: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = (*prefix, node.name)
                definitions.add(".".join(qualified))
                body = getattr(node, "body", None)
                if isinstance(body, list):
                    visit(body, qualified)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        add_binding(target.id, prefix)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                add_binding(node.target.id, prefix)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    add_binding(alias.asname or alias.name.split(".", 1)[0], prefix)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        add_binding(alias.asname or alias.name, prefix)

    visit(tree.body)
    return definitions


def _config_pointer_segments(symbol: str) -> list[str] | None:
    """Decode the repo-wide ``config:/...`` RFC-6901 symbol vocabulary."""

    if not symbol.startswith("config:/"):
        return None
    raw_segments = symbol.removeprefix("config:/").split("/")
    if not raw_segments or any(not segment for segment in raw_segments):
        return None
    segments: list[str] = []
    for raw in raw_segments:
        index = 0
        decoded: list[str] = []
        while index < len(raw):
            if raw[index] != "~":
                decoded.append(raw[index])
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                return None
            decoded.append("~" if raw[index + 1] == "0" else "/")
            index += 2
        segments.append("".join(decoded))
    return segments


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate config key: {key}")
        result[key] = value
    return result


def _config_value_at_pointer(value: Any, segments: list[str]) -> bool:
    current = value
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                return False
            current = current[segment]
            continue
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", segment):
                return False
            index = int(segment)
            if index >= len(current):
                return False
            current = current[index]
            continue
        return False
    return True


def _config_value_for_symbol(*, path: Path, symbol: str) -> tuple[bool, Any, str | None]:
    """Return one JSON-compatible config value for a verified ``config:/`` symbol.

    Outcome verification must not turn a static trace into a behavioral claim.  A
    config-state oracle therefore retains the exact typed value at the research
    revision and later compares that same pointer with a built-in parser.
    """

    segments = _config_pointer_segments(symbol)
    if segments is None or not path.is_file() or path.is_symlink():
        return False, None, None
    suffix = path.suffix.casefold()
    try:
        content = path.read_text(encoding="utf-8")
        if suffix == ".json":
            value = json.loads(content, object_pairs_hook=_unique_json_object)
            format_name = "json"
        elif suffix == ".toml":
            value = tomllib.loads(content)
            format_name = "toml"
        elif suffix in {".yaml", ".yml"}:
            # ``compose`` lets us reject duplicate keys along the selected path;
            # SafeLoader supplies the typed scalar/list/object value.
            if not _yaml_pointer_exists(content, segments):
                return False, None, None
            value = yaml.safe_load(content)
            format_name = "yaml"
        else:
            return False, None, None
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return False, None, None
    cursor = value
    for segment in segments:
        if isinstance(cursor, dict) and segment in cursor:
            cursor = cursor[segment]
        elif (
            isinstance(cursor, list)
            and re.fullmatch(r"0|[1-9][0-9]*", segment)
            and int(segment) < len(cursor)
        ):
            cursor = cursor[int(segment)]
        else:
            return False, None, None
    try:
        _canonical_json_sha256(cursor)
    except (TypeError, ValueError):
        return False, None, None
    return True, cursor, format_name


def _yaml_pointer_exists(content: str, segments: list[str]) -> bool:
    try:
        node = yaml.compose(content, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return False
    if node is None:
        return False
    current: Any = node
    for segment in segments:
        if isinstance(current, yaml.MappingNode):
            matches = [
                value_node
                for key_node, value_node in current.value
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == segment
            ]
            if len(matches) != 1:
                return False
            current = matches[0]
            continue
        if isinstance(current, yaml.SequenceNode):
            if not re.fullmatch(r"0|[1-9][0-9]*", segment):
                return False
            index = int(segment)
            if index >= len(current.value):
                return False
            current = current.value[index]
            continue
        return False
    return True


def _config_pointer_exists(*, path: str, content: str, symbol: str) -> bool:
    segments = _config_pointer_segments(symbol)
    if segments is None:
        return False
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.casefold()
    if suffix in {".yaml", ".yml"}:
        return _yaml_pointer_exists(content, segments)
    try:
        if suffix == ".json":
            value = json.loads(content, object_pairs_hook=_unique_json_object)
        elif suffix == ".toml":
            value = tomllib.loads(content)
        else:
            return False
    except ValueError:
        return False
    return _config_value_at_pointer(value, segments)


def _symbol_definition_exists(*, path: str, content: str, symbol: str) -> bool:
    if symbol.startswith("config:"):
        return _config_pointer_exists(path=path, content=content, symbol=symbol)
    components = [part for part in re.split(r"[.:#]", symbol) if part]
    if not components:
        return False
    if path.endswith(".py"):
        definitions = _python_definitions(content)
        path_object = PurePosixPath(path.replace("\\", "/"))
        module_qualifiers = {path_object.stem, *path_object.with_suffix("").parts}
        expected_components = (
            components[-1:]
            if len(components) >= 2 and components[-2] in module_qualifiers
            else components[-2:]
            if len(components) >= 2
            else components
        )
        expected = ".".join(expected_components)
        return expected in definitions or any(
            definition.endswith(f".{expected}") for definition in definitions
        )
    leaf = re.escape(components[-1])
    definition_patterns = (
        rf"\b(?:function|class|interface|struct|enum|def|fn)\s+{leaf}\b",
        rf"\b{leaf}\s*\([^)]*\)\s*(?:\{{|=>)",
    )
    return any(re.search(pattern, content) for pattern in definition_patterns)


def _pytest_args(executed_argv: list[str]) -> list[str] | None:
    normalized = tuple(token.casefold() for token in executed_argv)
    for prefix in _PYTEST_ARGV_PREFIXES:
        if normalized[: len(prefix)] == prefix:
            return executed_argv[len(prefix) :]
    return None


def _exact_pytest_selector(executed_argv: list[str]) -> tuple[str, list[str]] | None:
    """Resolve one unambiguous pytest file/function selector from executed argv."""
    arguments = _pytest_args(executed_argv)
    if arguments is None:
        return None
    for argument in arguments:
        lowered = argument.casefold()
        if (
            lowered in _PYTEST_AMBIGUOUS_SELECTION_OPTIONS
            or any(
                lowered.startswith(f"{option}=") for option in _PYTEST_AMBIGUOUS_SELECTION_OPTIONS
            )
            or (lowered.startswith("-k") and not lowered.startswith("--"))
            or (lowered.startswith("-m") and not lowered.startswith("--"))
        ):
            return None
        if ".usertest_research" in argument.replace("\\", "/").casefold():
            return None
    candidates = [argument for argument in arguments if "::" in argument]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    # Fail closed when another positional argument could change collection. Options
    # with separate values are intentionally unsupported; use ``--option=value``.
    if any(argument != candidate and not argument.startswith("-") for argument in arguments):
        return None
    path_raw, *selector_parts = candidate.split("::")
    normalized_path = path_raw.replace("\\", "/").removeprefix("./")
    if (
        not normalized_path.endswith(".py")
        or not selector_parts
        or any(not part.isidentifier() for part in selector_parts)
        or not selector_parts[-1].startswith("test")
    ):
        return None
    path = PurePosixPath(normalized_path)
    windows_path = PureWindowsPath(path_raw)
    if path.is_absolute() or windows_path.is_absolute() or ".." in path.parts:
        return None
    return normalized_path, selector_parts


def _selected_test_function(
    tree: ast.Module,
    selector_parts: list[str],
) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef] | None:
    nodes: list[ast.stmt] = tree.body
    qualified: list[str] = []
    for index, selector in enumerate(selector_parts):
        match = next(
            (
                node
                for node in nodes
                if isinstance(
                    node,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                )
                and node.name == selector
            ),
            None,
        )
        if match is None:
            return None
        qualified.append(selector)
        final = index == len(selector_parts) - 1
        if final:
            if not isinstance(match, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return None
            return ".".join(qualified), match
        if not isinstance(match, ast.ClassDef):
            return None
        nodes = match.body
    return None


class _FunctionScopeVisitor(ast.NodeVisitor):
    """Collect imports and calls in one function, excluding nested definitions."""

    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.calls: list[ast.Call] = []
        self.aliases: dict[str, str] = {}
        self.bound_names: set[str] = set()
        self.loaded_names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)
        else:
            self.bound_names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)
        else:
            self.bound_names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.bound_names.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            self.loaded_names.add(node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self.bound_names.add(node.arg)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if isinstance(node.name, str):
            self.bound_names.add(node.name)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            branch = node.body if node.test.value else node.orelse
            for child in branch:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            target = alias.name if alias.asname else local
            self.aliases[local] = target

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            return
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self.aliases[local] = ".".join(part for part in (module, alias.name) if part)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(node)
        self.generic_visit(node)


def _module_import_aliases(tree: ast.Module) -> dict[str, str]:
    visitor = _FunctionScopeVisitor(tree)
    visitor.generic_visit(tree)
    return visitor.aliases


def _test_module_functions(
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


def _dotted_expression(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_expression(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _reachable_test_functions(
    *,
    selected_name: str,
    selected_node: ast.FunctionDef | ast.AsyncFunctionDef,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    pending = [(selected_name, selected_node)]
    reached: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    while pending:
        function_name, function_node = pending.pop(0)
        if function_name in reached:
            continue
        reached[function_name] = function_node
        visitor = _FunctionScopeVisitor(function_node)
        visitor.visit(function_node)
        class_name = function_name.rsplit(".", 1)[0] if "." in function_name else None
        for call in visitor.calls:
            dotted = _dotted_expression(call.func)
            helper_name: str | None = None
            if dotted in functions:
                helper_name = dotted
            elif class_name is not None and dotted is not None:
                for prefix in ("self.", "cls."):
                    if dotted.startswith(prefix):
                        candidate = f"{class_name}.{dotted.removeprefix(prefix)}"
                        if candidate in functions:
                            helper_name = candidate
                        break
            if helper_name is not None and helper_name not in reached:
                pending.append((helper_name, functions[helper_name]))
    return sorted(reached.items())


def _reachable_function_contracts(
    reachable: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
) -> list[dict[str, str]]:
    """Hash the executable AST closure selected by one exact pytest node."""

    return [
        {
            "function": name,
            "function_ast_sha256": sha256(
                ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8")
            ).hexdigest(),
        }
        for name, node in reachable
    ]


def _relevant_module_imports_projection(
    tree: ast.Module,
    reachable: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
) -> list[dict[str, Any]]:
    """Project only module imports referenced by the reachable test closure."""

    loaded: set[str] = set()
    for _, node in reachable:
        visitor = _FunctionScopeVisitor(node)
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


def _mechanism_call_targets(*, symbol: str, source_path: str) -> set[str]:
    components = [part for part in re.split(r"[.:#]", symbol) if part]
    path = PurePosixPath(source_path.replace("\\", "/")).with_suffix("")
    module_parts = list(path.parts)
    module_candidates = {
        ".".join(module_parts[index:]) for index in range(len(module_parts)) if module_parts[index:]
    }
    module_components = {part for module in module_candidates for part in module.split(".")}
    if len(components) >= 2 and components[-2] not in module_components:
        definition_tail = ".".join(components[-2:])
    else:
        definition_tail = components[-1] if components else ""
    targets = {symbol.replace(":", ".").replace("#", ".")}
    targets.update(
        f"{module}.{definition_tail}" for module in module_candidates if module and definition_tail
    )
    return targets


def _resolved_call_expression(expression: str, aliases: dict[str, str]) -> str | None:
    first, separator, remainder = expression.partition(".")
    target = aliases.get(first)
    if target is None:
        return None
    return f"{target}{separator}{remainder}" if separator else target


def _research_harness_relative_path(executed_argv: Any) -> str | None:
    """Return the retained Python harness selected by one replay argv, if any."""

    if not isinstance(executed_argv, list) or any(
        not isinstance(argument, str) for argument in executed_argv
    ):
        return None
    normalized = [argument.casefold() for argument in executed_argv]
    index: int | None = None
    if normalized[:3] == ["pdm", "run", "python"]:
        index = 3
    elif normalized[:1] == ["python"]:
        index = 1
    if index is None or index >= len(executed_argv):
        return None
    candidate = executed_argv[index].replace("\\", "/")
    path = PurePosixPath(candidate)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 2
        or path.parts[0] != ".usertest_research"
        or path.suffix.casefold() != ".py"
    ):
        return None
    return path.as_posix()


def _harness_mechanism_touches(
    *,
    replay: dict[str, Any],
    mechanism_symbols: list[str],
    symbol_paths: dict[str, str],
    observable_assertion: dict[str, Any],
) -> tuple[str | None, list[str], dict[str, Any] | None]:
    """Verify that a retained harness exposes production-call data to the oracle.

    Merely calling a symbol and then printing a hard-coded symptom is not evidence.
    A call must feed a stream/exit/assertion sink directly, or through an assigned
    local that feeds such a sink.  This remains intentionally lightweight so a
    focused temporary harness is usable without manufacturing a pytest-shaped test.
    """

    relative = _research_harness_relative_path(replay.get("executed_argv"))
    if relative is None:
        return None, [], None
    workspace_raw = _text(replay.get("workspace_dir"))
    if workspace_raw is None:
        return relative, [], None
    workspace = Path(workspace_raw).resolve()
    path = (workspace / relative).resolve()
    if not _within(path, workspace) or not path.is_file() or path.is_symlink():
        return relative, [], None
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(content)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return relative, [], None

    aliases = _module_import_aliases(tree)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    assertion_source = str(observable_assertion.get("source") or "")

    def call_name(call: ast.Call) -> str:
        return (_dotted_expression(call.func) or "").casefold()

    def observable_sink(node: ast.AST) -> str | None:
        cursor: ast.AST | None = node
        while cursor is not None:
            parent = parents.get(cursor)
            if isinstance(parent, ast.Assert):
                return "exit_code"
            if isinstance(parent, ast.Raise):
                return "stderr"
            if isinstance(parent, ast.Call):
                name = call_name(parent)
                if name in {"print", "builtins.print"}:
                    stream = "stdout"
                    for keyword in parent.keywords:
                        if keyword.arg == "file":
                            target = (_dotted_expression(keyword.value) or "").casefold()
                            if target == "sys.stderr":
                                stream = "stderr"
                    return stream
                if name in {"sys.exit", "exit", "builtins.exit"}:
                    return "exit_code"
                if name.endswith(".write") or name.endswith(".write_text"):
                    if name.startswith("sys.stderr"):
                        return "stderr"
                    if name.startswith("sys.stdout"):
                        return "stdout"
                    return "artifact"
            cursor = parent
        return None

    def assigned_names(call: ast.Call) -> set[str]:
        parent = parents.get(call)
        targets: list[ast.AST] = []
        if isinstance(parent, ast.Assign) and parent.value is call:
            targets.extend(parent.targets)
        elif isinstance(parent, ast.AnnAssign) and parent.value is call:
            targets.append(parent.target)
        elif isinstance(parent, ast.NamedExpr) and parent.value is call:
            targets.append(parent.target)
        names: set[str] = set()
        for target in targets:
            for candidate in ast.walk(target):
                if isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Store):
                    names.add(candidate.id)
        return names

    def asserted_value_is_hard_coded(dynamic_node: ast.AST) -> bool:
        expected = observable_assertion.get("expected")
        operator = observable_assertion.get("operator")
        if operator not in {"contains", "equals"} or not isinstance(expected, str):
            return False
        cursor: ast.AST | None = dynamic_node
        sink_node: ast.AST | None = None
        while cursor is not None:
            parent = parents.get(cursor)
            if isinstance(parent, (ast.Assert, ast.Raise)):
                sink_node = parent
                break
            if isinstance(parent, ast.Call) and observable_sink(cursor) is not None:
                sink_node = parent
                break
            cursor = parent
        if sink_node is None:
            return False

        literals: list[str] = []

        def collect(node: ast.AST) -> None:
            if node is dynamic_node:
                return
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
                return
            for child in ast.iter_child_nodes(node):
                collect(child)

        collect(sink_node)
        static_text = "".join(literals)
        return expected in static_text

    def linked_sink(call: ast.Call) -> tuple[str, ast.AST] | None:
        direct = observable_sink(call)
        if direct is not None:
            return direct, call
        names = assigned_names(call)
        if not names:
            return None
        for candidate in ast.walk(tree):
            if (
                isinstance(candidate, ast.Name)
                and isinstance(candidate.ctx, ast.Load)
                and candidate.id in names
            ):
                sink = observable_sink(candidate)
                if sink is not None:
                    return sink, candidate
        return None

    touched: set[str] = set()
    sinks: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        expression = _dotted_expression(node.func)
        if expression is None:
            continue
        resolved = _resolved_call_expression(expression, aliases)
        for symbol in mechanism_symbols:
            source_path = symbol_paths.get(symbol)
            if source_path is None:
                continue
            targets = _mechanism_call_targets(symbol=symbol, source_path=source_path)
            if expression in targets or resolved in targets:
                linked = linked_sink(node)
                sink = linked[0] if linked is not None else None
                dynamic_node = linked[1] if linked is not None else None
                source_matches = (
                    (assertion_source == "combined" and sink in {"stdout", "stderr"})
                    or sink == assertion_source
                    or (assertion_source == "exit_code" and sink in {"exit_code", "stderr"})
                )
                if (
                    sink is not None
                    and source_matches
                    and dynamic_node is not None
                    and not asserted_value_is_hard_coded(dynamic_node)
                ):
                    touched.add(symbol)
                    sinks[symbol] = sink
    ordered = sorted(touched)
    link = None
    if ordered:
        link = {
            "verification_method": "runner_harness_observable_dataflow_v1",
            "entrypoint": relative,
            "observable_source": assertion_source,
            "symbol_sinks": [{"symbol": symbol, "sink": sinks[symbol]} for symbol in ordered],
        }
    return relative, ordered, link


def _verified_declared_mechanism_link(
    *,
    experiment: dict[str, Any],
    mechanism_symbols: list[str],
    symbol_paths: dict[str, str],
    workspace: Path | None,
) -> dict[str, Any] | None:
    """Derive a Python call chain from inspected baseline source.

    Model prose is never treated as the edge.  Every adjacent step must be a direct
    import-resolved call in the declared caller body, and the receipt retains its
    source line and AST hash.  Other languages/config formats use a harness, static
    trace, exception trace, or controlled proof instead of a fabricated chain.
    """

    raw = experiment.get("mechanism_link")
    if not isinstance(raw, dict) or raw.get("kind") != "entrypoint_dataflow":
        return None
    entrypoint = _text(raw.get("entrypoint"))
    steps_raw = raw.get("code_path")
    steps = steps_raw if isinstance(steps_raw, list) else []
    if entrypoint is None or not steps:
        return None
    if workspace is None:
        return None
    projected: list[dict[str, str]] = []
    for step in steps:
        if not isinstance(step, dict):
            return None
        symbol = _text(step.get("symbol"))
        path = _text(step.get("path"))
        observation = _text(step.get("observation"))
        if (
            symbol is None
            or path is None
            or observation is None
            or symbol_paths.get(symbol) != path
        ):
            return None
        projected.append({"symbol": symbol, "path": path, "observation": observation})
    if (
        projected[0]["symbol"] != entrypoint
        or not set(mechanism_symbols).issubset({step["symbol"] for step in projected})
        or len(projected) < 2
    ):
        return None

    def symbol_node(tree: ast.Module, symbol: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        candidates: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

        def visit(nodes: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = ".".join((*prefix, node.name))
                    candidates.append((qualified, node))
                if isinstance(node, ast.ClassDef):
                    visit(node.body, (*prefix, node.name))

        visit(tree.body)
        tail = symbol.replace(":", ".").replace("#", ".")
        matches = [node for name, node in candidates if tail.endswith(name)]
        return matches[0] if len(matches) == 1 else None

    edges: list[dict[str, Any]] = []
    for caller, callee in zip(projected, projected[1:], strict=False):
        caller_path = (workspace / caller["path"]).resolve()
        if (
            not _within(caller_path, workspace.resolve())
            or caller_path.suffix.casefold() != ".py"
            or not caller_path.is_file()
            or caller_path.is_symlink()
        ):
            return None
        try:
            content = caller_path.read_text(encoding="utf-8", errors="strict")
            tree = ast.parse(content)
        except (OSError, UnicodeDecodeError, SyntaxError):
            return None
        node = symbol_node(tree, caller["symbol"])
        if node is None:
            return None
        module_aliases = _module_import_aliases(tree)
        visitor = _FunctionScopeVisitor(node)
        visitor.visit(node)
        aliases = {**module_aliases, **visitor.aliases}
        targets = _mechanism_call_targets(symbol=callee["symbol"], source_path=callee["path"])
        if caller["path"] == callee["path"] and symbol_node(tree, callee["symbol"]) is not None:
            targets.add(callee["symbol"].replace(":", ".").replace("#", ".").rsplit(".", 1)[-1])
        matched_call: ast.Call | None = None
        matched_expression: str | None = None
        for call in visitor.calls:
            expression = _dotted_expression(call.func)
            if expression is None:
                continue
            resolved = _resolved_call_expression(expression, aliases)
            if expression in targets or resolved in targets:
                matched_call = call
                matched_expression = resolved or expression
                break
        if matched_call is None or matched_expression is None:
            return None
        call_projection = ast.dump(matched_call, annotate_fields=True, include_attributes=False)
        edges.append(
            {
                "caller_symbol": caller["symbol"],
                "caller_path": caller["path"],
                "callee_symbol": callee["symbol"],
                "callee_path": callee["path"],
                "line": matched_call.lineno,
                "resolved_call": matched_expression,
                "call_ast_sha256": sha256(call_projection.encode()).hexdigest(),
            }
        )
    link = {
        "verification_method": "runner_python_call_chain_v1",
        "entrypoint": entrypoint,
        "code_path": projected,
        "verified_call_edges": edges,
    }
    link["mechanism_link_sha256"] = _canonical_json_sha256(link)
    return link


def _experiment_consumer_identity(
    *,
    experiment: dict[str, Any],
    replay: dict[str, Any],
    mechanism_link: dict[str, Any] | None,
    harness_path: str | None,
) -> dict[str, Any]:
    """Derive breadth identity from a consumer/entrypoint, never command count."""

    # A retained research harness is the consumer that was actually executed.  A
    # runner-verified observation that the harness reaches production code is
    # useful causal evidence, but it does not turn the harness into a production
    # entrypoint.  Keeping those identities distinct prevents breadth claims from
    # being manufactured by adding more temporary probes.
    if harness_path is not None:
        return {"kind": "research_harness", "entrypoint": harness_path}
    if isinstance(mechanism_link, dict):
        entrypoint = _text(mechanism_link.get("entrypoint"))
        if entrypoint is not None:
            if entrypoint.replace("\\", "/").startswith(".usertest_research/"):
                return {"kind": "research_harness", "entrypoint": entrypoint}
            return content_bound_payload(
                {
                    "kind": "runner_observed_entrypoint",
                    "entrypoint": entrypoint,
                    "attestation_basis": "runner_mechanism_link",
                    "runner_attested": True,
                },
                hash_field="consumer_identity_sha256",
            )
    selection = _exact_pytest_selector(replay.get("executed_argv", []))
    if selection is not None:
        test_path, selector_parts = selection
        return {
            "kind": "evidence_selector",
            "entrypoint": f"{test_path}::{':'.join(selector_parts)}",
        }
    return {
        "kind": "unresolved_consumer",
        "entrypoint": str(experiment.get("experiment_id") or "unknown"),
    }


def _call_argument_receipts(call: ast.Call, *, content: str) -> tuple[list[dict[str, Any]], bool]:
    """Project one call's explicit arguments into stable, runner-owned AST receipts.

    Starred arguments and ``**kwargs`` cannot be paired to one structural input slot
    without evaluating Python.  They are retained as incomplete instead of being
    guessed; causal-control verification will fail closed on such a call.
    """

    arguments: list[dict[str, Any]] = []
    complete = True
    for index, argument in enumerate(call.args):
        if isinstance(argument, ast.Starred):
            complete = False
            continue
        projection = ast.dump(argument, annotate_fields=True, include_attributes=False)
        arguments.append(
            {
                "slot": f"positional:{index}",
                "expression": ast.get_source_segment(content, argument) or ast.unparse(argument),
                "ast_sha256": sha256(projection.encode("utf-8")).hexdigest(),
            }
        )
    for keyword in call.keywords:
        if keyword.arg is None:
            complete = False
            continue
        projection = ast.dump(keyword.value, annotate_fields=True, include_attributes=False)
        arguments.append(
            {
                "slot": f"keyword:{keyword.arg}",
                "expression": (
                    ast.get_source_segment(content, keyword.value) or ast.unparse(keyword.value)
                ),
                "ast_sha256": sha256(projection.encode("utf-8")).hexdigest(),
            }
        )
    arguments.sort(key=lambda item: str(item["slot"]))
    return arguments, complete


def _repository_test_semantic_assertions(
    *,
    content: str,
    reachable: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
    module_aliases: dict[str, str],
    mechanism_symbols: list[str],
    symbol_paths: dict[str, str],
) -> list[dict[str, Any]]:
    """Bind existing pytest assertions to values produced by mechanism calls.

    A passing pytest exit is meaningful only when the selected repository test
    already asserts behavior that depends on the researched mechanism.  This
    deliberately supports a small, auditable data-flow vocabulary: a mechanism
    call directly inside an ``assert`` expression, or a simple local name assigned
    from such a call and subsequently loaded by an ``assert``.  Prose and unrelated
    ``assert True`` statements never become a semantic success contract.
    """

    targets_by_symbol = {
        symbol: _mechanism_call_targets(symbol=symbol, source_path=symbol_paths[symbol])
        for symbol in mechanism_symbols
        if symbol in symbol_paths
    }

    def expression_symbols(node: ast.AST, aliases: dict[str, str]) -> set[str]:
        observed: set[str] = set()
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Call):
                continue
            expression = _dotted_expression(candidate.func)
            if expression is None:
                continue
            resolved = _resolved_call_expression(expression, aliases)
            for symbol, targets in targets_by_symbol.items():
                if expression in targets or resolved in targets:
                    observed.add(symbol)
        return observed

    class SemanticVisitor(ast.NodeVisitor):
        def __init__(self, root: ast.AST) -> None:
            self.root = root
            self.assignments: list[ast.Assign | ast.AnnAssign] = []
            self.assertions: list[ast.Assert] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is self.root:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is self.root:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            del node

        def visit_Assign(self, node: ast.Assign) -> None:
            self.assignments.append(node)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self.assignments.append(node)
            self.generic_visit(node)

        def visit_Assert(self, node: ast.Assert) -> None:
            self.assertions.append(node)
            self.generic_visit(node)

    receipts: list[dict[str, Any]] = []
    for function_name, function_node in reachable:
        scope = _FunctionScopeVisitor(function_node)
        scope.visit(function_node)
        aliases = {**module_aliases, **scope.aliases}
        for bound_name in scope.bound_names - set(scope.aliases):
            aliases.pop(bound_name, None)
        visitor = SemanticVisitor(function_node)
        visitor.visit(function_node)
        assigned_symbols: dict[str, set[str]] = {}
        for assignment in visitor.assignments:
            value = assignment.value
            if value is None:
                continue
            symbols = expression_symbols(value, aliases)
            if not symbols:
                continue
            targets: list[ast.expr] = (
                list(assignment.targets)
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    assigned_symbols.setdefault(target.id, set()).update(symbols)
        for assertion in visitor.assertions:
            symbols = expression_symbols(assertion.test, aliases)
            loaded_names = {
                candidate.id
                for candidate in ast.walk(assertion.test)
                if isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Load)
            }
            for name in loaded_names:
                symbols.update(assigned_symbols.get(name, set()))
            if not symbols:
                continue
            projection = ast.dump(
                assertion.test,
                annotate_fields=True,
                include_attributes=False,
            )
            receipts.append(
                {
                    "function": function_name,
                    "line": assertion.lineno,
                    "expression": (
                        ast.get_source_segment(content, assertion.test)
                        or ast.unparse(assertion.test)
                    ),
                    "assertion_ast_sha256": sha256(projection.encode("utf-8")).hexdigest(),
                    "mechanism_symbols": sorted(symbols),
                }
            )
    return sorted(
        receipts,
        key=lambda item: (str(item.get("function")), int(item.get("line", 0))),
    )


def _pytest_test_selection_receipt(
    *,
    hypothesis_id: str,
    experiment_id: str,
    experiment: dict[str, Any],
    replay: dict[str, Any],
    mechanism_symbols: list[str],
    symbol_paths: dict[str, str],
    planning_workspace: Path,
    errors: list[str],
) -> dict[str, Any] | None:
    command = _text(experiment.get("command"))
    executed_raw = replay.get("executed_argv")
    executed_argv = executed_raw if isinstance(executed_raw, list) else []
    if (
        command is None
        or any(not isinstance(argument, str) for argument in executed_argv)
        or _parse_replay_argv(command) != executed_argv
    ):
        errors.append(f"causal_control_command_unverified:{hypothesis_id}:{experiment_id}")
        return None
    selection = _exact_pytest_selector(executed_argv)
    if selection is None:
        errors.append(
            f"causal_control_exact_pytest_selector_required:{hypothesis_id}:{experiment_id}"
        )
        return None
    test_path, selector_parts = selection
    path = (planning_workspace / test_path).resolve()
    if not _within(path, planning_workspace.resolve()) or not path.is_file() or path.is_symlink():
        errors.append(
            f"causal_control_test_file_unavailable:{hypothesis_id}:{experiment_id}:{test_path}"
        )
        return None
    git_blob_sha = _git_blob_sha(planning_workspace, test_path)
    if git_blob_sha is None:
        errors.append(
            f"causal_control_test_file_not_in_baseline:{hypothesis_id}:{experiment_id}:{test_path}"
        )
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        errors.append(
            f"causal_control_test_file_unreadable:{hypothesis_id}:{experiment_id}:{test_path}"
        )
        return None
    try:
        tree = ast.parse(content)
    except SyntaxError:
        errors.append(
            f"causal_control_test_file_ast_invalid:{hypothesis_id}:{experiment_id}:{test_path}"
        )
        return None
    selected = _selected_test_function(tree, selector_parts)
    if selected is None:
        errors.append(
            f"causal_control_test_selector_unresolved:{hypothesis_id}:"
            f"{experiment_id}:{test_path}::{':'.join(selector_parts)}"
        )
        return None
    selected_name, selected_node = selected
    functions = _test_module_functions(tree)
    reachable = _reachable_test_functions(
        selected_name=selected_name,
        selected_node=selected_node,
        functions=functions,
    )
    module_aliases = _module_import_aliases(tree)
    mechanism_touches: list[dict[str, Any]] = []
    for symbol in mechanism_symbols:
        source_path = symbol_paths.get(symbol)
        if source_path is None:
            errors.append(
                f"causal_control_mechanism_symbol_unresolved:"
                f"{hypothesis_id}:{experiment_id}:{symbol}"
            )
            continue
        targets = _mechanism_call_targets(symbol=symbol, source_path=source_path)
        calls: list[dict[str, Any]] = []
        for function_name, function_node in reachable:
            visitor = _FunctionScopeVisitor(function_node)
            visitor.visit(function_node)
            aliases = {**module_aliases, **visitor.aliases}
            for bound_name in visitor.bound_names - set(visitor.aliases):
                aliases.pop(bound_name, None)
            for call in visitor.calls:
                expression = _dotted_expression(call.func)
                if expression is None:
                    continue
                resolved = _resolved_call_expression(expression, aliases)
                if resolved not in targets:
                    continue
                arguments, arguments_complete = _call_argument_receipts(
                    call,
                    content=content,
                )
                calls.append(
                    {
                        "function": function_name,
                        "line": call.lineno,
                        "expression": expression,
                        "resolved_target": resolved,
                        "arguments": arguments,
                        "arguments_complete": arguments_complete,
                    }
                )
        if not calls:
            errors.append(
                f"causal_control_mechanism_not_called:{hypothesis_id}:{experiment_id}:{symbol}"
            )
            continue
        mechanism_touches.append(
            {
                "symbol": symbol,
                "source_path": source_path,
                "calls": calls,
            }
        )
    source_segment = ast.get_source_segment(content, selected_node) or ""
    relevant_imports = _relevant_module_imports_projection(tree, reachable)
    semantic_assertions = _repository_test_semantic_assertions(
        content=content,
        reachable=reachable,
        module_aliases=module_aliases,
        mechanism_symbols=mechanism_symbols,
        symbol_paths=symbol_paths,
    )
    return {
        "selection_id": f"{hypothesis_id}:{experiment_id}",
        "hypothesis_id": hypothesis_id,
        "experiment_id": experiment_id,
        "runner": "pytest",
        "command_sha256": sha256(command.encode("utf-8")).hexdigest(),
        "executed_argv_sha256": _canonical_json_sha256(executed_argv),
        "test_path": test_path,
        "test_file_sha256": _sha256_path(path),
        "test_file_git_blob_sha": git_blob_sha,
        "selector": "::".join(selector_parts),
        "selector_parts": selector_parts,
        "test_function": selected_name,
        "test_function_line": selected_node.lineno,
        "test_function_source_sha256": sha256(source_segment.encode("utf-8")).hexdigest(),
        "reachable_function_contracts": _reachable_function_contracts(reachable),
        "relevant_module_imports_sha256": _canonical_json_sha256(relevant_imports),
        "reachable_functions": [name for name, _ in reachable],
        "mechanism_touches": mechanism_touches,
        "semantic_assertions": semantic_assertions,
    }


def _mechanism_calls_by_symbol(selection: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    calls_by_symbol: dict[str, list[dict[str, Any]]] = {}
    touches_raw = selection.get("mechanism_touches")
    touches = touches_raw if isinstance(touches_raw, list) else []
    for touch in touches:
        if not isinstance(touch, dict):
            continue
        symbol = _text(touch.get("symbol"))
        calls_raw = touch.get("calls")
        if symbol is not None and isinstance(calls_raw, list):
            calls_by_symbol[symbol] = [call for call in calls_raw if isinstance(call, dict)]
    return calls_by_symbol


def _structural_controlled_difference(
    *,
    hypothesis_id: str,
    control_id: str,
    mechanism_symbols: list[str],
    support_selection: dict[str, Any],
    control_selection: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    """Prove that support/control calls differ in exactly one explicit AST slot."""

    support_calls = _mechanism_calls_by_symbol(support_selection)
    control_calls = _mechanism_calls_by_symbol(control_selection)
    differences: list[dict[str, Any]] = []
    for symbol in mechanism_symbols:
        support_candidates = support_calls.get(symbol, [])
        control_candidates = control_calls.get(symbol, [])
        if len(support_candidates) != 1 or len(control_candidates) != 1:
            errors.append(
                "causal_control_mechanism_call_ambiguous:"
                f"{hypothesis_id}:{control_id}:{symbol}:"
                f"{len(support_candidates)}:{len(control_candidates)}"
            )
            return None
        support_call = support_candidates[0]
        control_call = control_candidates[0]
        if (
            support_call.get("arguments_complete") is not True
            or control_call.get("arguments_complete") is not True
        ):
            errors.append(
                f"causal_control_argument_projection_incomplete:"
                f"{hypothesis_id}:{control_id}:{symbol}"
            )
            return None
        support_arguments = {
            str(argument.get("slot")): argument
            for argument in support_call.get("arguments", [])
            if isinstance(argument, dict) and _text(argument.get("slot")) is not None
        }
        control_arguments = {
            str(argument.get("slot")): argument
            for argument in control_call.get("arguments", [])
            if isinstance(argument, dict) and _text(argument.get("slot")) is not None
        }
        for slot in sorted(set(support_arguments) | set(control_arguments)):
            support_argument = support_arguments.get(slot)
            control_argument = control_arguments.get(slot)
            if (
                isinstance(support_argument, dict)
                and isinstance(control_argument, dict)
                and support_argument.get("ast_sha256") == control_argument.get("ast_sha256")
            ):
                continue
            differences.append(
                {
                    "mechanism_symbol": symbol,
                    "slot": slot,
                    "difference_kind": (
                        "added_in_control"
                        if support_argument is None
                        else "removed_in_control"
                        if control_argument is None
                        else "changed"
                    ),
                    "support_argument": support_argument,
                    "control_argument": control_argument,
                }
            )
    if len(differences) != 1:
        errors.append(
            f"causal_control_requires_exactly_one_structural_difference:"
            f"{hypothesis_id}:{control_id}:{len(differences)}"
        )
        return None
    return {
        "verification_method": "python_ast_explicit_argument_delta_v1",
        "difference_count": 1,
        "difference": differences[0],
    }


def _declared_mechanism_subset(
    value: Any,
    *,
    hypothesis_symbols: list[str],
) -> list[str] | None:
    """Normalize one non-empty declared subset in hypothesis-path order."""

    if not isinstance(value, list):
        return None
    declared = [symbol.strip() for symbol in value if isinstance(symbol, str) and symbol.strip()]
    if not declared or len(declared) != len(value) or len(set(declared)) != len(declared):
        return None
    hypothesis_set = set(hypothesis_symbols)
    if not set(declared).issubset(hypothesis_set):
        return None
    return [symbol for symbol in hypothesis_symbols if symbol in set(declared)]


def _selection_verified_symbols(
    selection: dict[str, Any] | None,
    *,
    requested_symbols: list[str],
) -> list[str]:
    if not isinstance(selection, dict):
        return []
    touched = {
        str(touch.get("symbol"))
        for touch in selection.get("mechanism_touches", [])
        if isinstance(touch, dict) and _text(touch.get("symbol")) is not None
    }
    return [symbol for symbol in requested_symbols if symbol in touched]


def _non_pytest_mechanism_proof_modes(
    *,
    experiment: dict[str, Any],
    replay: dict[str, Any],
    mechanism_symbols: list[str],
    symbol_paths: dict[str, str],
) -> set[str]:
    """Return independently observed proof modes covering every requested symbol.

    This helper is used only after the normal clean-replay and assertion gates.  It
    prevents an argv-level intervention from being promoted when the two commands
    merely switch execution modes while model prose claims they share a mechanism.
    """

    required = set(mechanism_symbols)
    if not required:
        return set()
    modes: set[str] = set()
    _harness_path, touched, _harness_link = _harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=mechanism_symbols,
        symbol_paths=symbol_paths,
        observable_assertion=(
            experiment.get("observable_assertion")
            if isinstance(experiment.get("observable_assertion"), dict)
            else {}
        ),
    )
    if required.issubset(set(touched)):
        modes.add("retained_harness_observable_dataflow")

    workspace_raw = _text(replay.get("workspace_dir"))
    declared_link = _verified_declared_mechanism_link(
        experiment=experiment,
        mechanism_symbols=mechanism_symbols,
        symbol_paths=symbol_paths,
        workspace=Path(workspace_raw) if workspace_raw is not None else None,
    )
    if isinstance(declared_link, dict):
        modes.add("declared_python_call_chain")

    traced: set[str] = set()
    for symbol in mechanism_symbols:
        relative_path = symbol_paths.get(symbol)
        if relative_path is None:
            continue
        for stream in ("stderr", "stdout"):
            output_path_raw = _text(replay.get(f"{stream}_path"))
            output_path = Path(output_path_raw) if output_path_raw is not None else None
            if output_path is None or not output_path.is_file():
                continue
            output = output_path.read_text(encoding="utf-8", errors="replace")
            if (
                _causal_trace_match(
                    output=output,
                    relative_path=relative_path,
                    symbol=symbol,
                )
                is not None
            ):
                traced.add(symbol)
                break
    if required.issubset(traced):
        modes.add("runner_exception_symbol_trace")
    return modes


def _replay_observed_value(
    replay: dict[str, Any],
    *,
    source: str,
) -> tuple[Any, str] | None:
    if source == "exit_code":
        exit_code = replay.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return None
        return exit_code, _canonical_json_sha256(exit_code)
    stream_paths: list[Path]
    if source in {"stdout", "stderr"}:
        raw_path = _text(replay.get(f"{source}_path"))
        stream_paths = [Path(raw_path)] if raw_path is not None else []
    elif source == "combined":
        stdout_path = _text(replay.get("stdout_path"))
        stderr_path = _text(replay.get("stderr_path"))
        stream_paths = (
            [Path(stdout_path), Path(stderr_path)]
            if stdout_path is not None and stderr_path is not None
            else []
        )
    else:
        return None
    if not stream_paths or any(not path.is_file() for path in stream_paths):
        return None
    try:
        observed = "".join(
            path.read_text(encoding="utf-8", errors="replace") for path in stream_paths
        )
    except OSError:
        return None
    return observed, sha256(observed.encode("utf-8")).hexdigest()


def _observable_controlled_difference(
    *,
    hypothesis_id: str,
    control_id: str,
    support: dict[str, Any],
    control: dict[str, Any],
    support_replay: dict[str, Any],
    control_replay: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    """Prove complementary support/control results from runner replay observations."""

    support_assertion_raw = support.get("observable_assertion")
    control_assertion_raw = control.get("observable_assertion")
    support_assertion = support_assertion_raw if isinstance(support_assertion_raw, dict) else {}
    control_assertion = control_assertion_raw if isinstance(control_assertion_raw, dict) else {}
    if (
        support_replay.get("assertion_passed") is not True
        or control_replay.get("assertion_passed") is not True
    ):
        errors.append(f"causal_control_replay_assertion_not_verified:{hypothesis_id}:{control_id}")
        return None
    source = _text(support_assertion.get("source"))
    if source is None or control_assertion.get("source") != source:
        errors.append(f"causal_control_observable_source_mismatch:{hypothesis_id}:{control_id}")
        return None
    support_observed = _replay_observed_value(support_replay, source=source)
    control_observed = _replay_observed_value(control_replay, source=source)
    if support_observed is None or control_observed is None:
        errors.append(
            f"causal_control_observable_unavailable:{hypothesis_id}:{control_id}:{source}"
        )
        return None
    support_value, support_hash = support_observed
    control_value, control_hash = control_observed
    difference_kind: str | None = None
    expected_sha256: str | None = None
    support_expected_sha256: str | None = None
    control_expected_sha256: str | None = None
    if source == "exit_code":
        if (
            support_assertion.get("operator") == "equals"
            and control_assertion.get("operator") == "equals"
            and support_assertion.get("expected") == support_value
            and control_assertion.get("expected") == control_value
            and isinstance(support_value, int)
            and not isinstance(support_value, bool)
            and support_value != 0
            and control_value == 0
        ):
            difference_kind = "failing_exit_to_zero"
    elif (
        support_assertion.get("operator") == "contains"
        and control_assertion.get("operator") == "not_contains"
        and isinstance(support_assertion.get("expected"), str)
        and support_assertion.get("expected") == control_assertion.get("expected")
    ):
        difference_kind = "failure_marker_removed"
        expected_sha256 = sha256(str(support_assertion["expected"]).encode("utf-8")).hexdigest()
    elif (
        source in {"stdout", "stderr", "combined"}
        and support_assertion.get("operator") == "equals"
        and control_assertion.get("operator") == "equals"
        and isinstance(support_assertion.get("expected"), str)
        and isinstance(control_assertion.get("expected"), str)
        and support_assertion.get("expected") != control_assertion.get("expected")
        and isinstance(support_value, str)
        and isinstance(control_value, str)
        and support_value.strip() == str(support_assertion["expected"]).strip()
        and control_value.strip() == str(control_assertion["expected"]).strip()
    ):
        # A zero-exit wrong result becoming the specifically correct result is
        # stronger evidence than merely suppressing an old marker.  Preserve
        # both declared values by hash in the runner-owned receipt.
        difference_kind = "wrong_value_corrected"
        support_expected_sha256 = _canonical_json_sha256(support_assertion["expected"])
        control_expected_sha256 = _canonical_json_sha256(control_assertion["expected"])
    if difference_kind is None or support_hash == control_hash:
        errors.append(f"causal_control_observable_not_complementary:{hypothesis_id}:{control_id}")
        return None
    return {
        "verification_method": "runner_replay_complement_v1",
        "source": source,
        "difference_kind": difference_kind,
        "expected_sha256": expected_sha256,
        "support_expected_sha256": support_expected_sha256,
        "control_expected_sha256": control_expected_sha256,
        "support": {
            "exit_code": support_replay.get("exit_code"),
            "observed_sha256": support_hash,
            "stdout_sha256": support_replay.get("stdout_sha256"),
            "stderr_sha256": support_replay.get("stderr_sha256"),
        },
        "control": {
            "exit_code": control_replay.get("exit_code"),
            "observed_sha256": control_hash,
            "stdout_sha256": control_replay.get("stdout_sha256"),
            "stderr_sha256": control_replay.get("stderr_sha256"),
        },
    }


def _content_addressed_receipt_id(prefix: str, receipt: dict[str, Any], id_field: str) -> str:
    projection = {key: value for key, value in receipt.items() if key != id_field}
    return f"{prefix}:{_canonical_json_sha256(projection)}"


def _failure_path_receipts(
    dossier: dict[str, Any],
    *,
    test_selections: list[dict[str, Any]],
    control_verifications: list[dict[str, Any]],
    errors: list[str],
) -> list[dict[str, Any]]:
    """Mint runner-owned path identities only from verified causal controls."""

    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    selections = {
        str(selection.get("selection_id")): selection
        for selection in test_selections
        if isinstance(selection, dict) and _text(selection.get("selection_id")) is not None
    }
    paths: list[dict[str, Any]] = []
    for control_verification in control_verifications:
        support_id = _text(control_verification.get("support_experiment_id"))
        support = experiments.get(support_id or "")
        support_selection = selections.get(
            str(control_verification.get("support_selection_id") or "")
        )
        control_verification_id = _text(control_verification.get("control_verification_id"))
        if (
            support_id is None
            or not isinstance(support, dict)
            or not isinstance(support_selection, dict)
            or control_verification_id is None
        ):
            errors.append("failure_path_control_projection_unavailable")
            continue
        origin_atom_ids_raw = support.get("addresses_atom_ids")
        origin_atom_ids = sorted(
            {
                atom_id.strip()
                for atom_id in (
                    origin_atom_ids_raw if isinstance(origin_atom_ids_raw, list) else []
                )
                if isinstance(atom_id, str) and atom_id.strip()
            }
        )
        if not origin_atom_ids:
            errors.append(f"failure_path_origin_atoms_missing:{support_id}")
            continue
        test_path = str(support_selection.get("test_path") or "")
        selector = str(support_selection.get("selector") or "")
        path_name = f"{test_path}::{selector}"
        consumer_identity = {
            "kind": "evidence_selector",
            "entrypoint": path_name,
        }
        observable = control_verification.get("observable_difference")
        support_observation = observable.get("support") if isinstance(observable, dict) else None
        if not isinstance(support_observation, dict):
            errors.append(f"failure_path_observation_missing:{support_id}")
            continue
        path_receipt: dict[str, Any] = {
            "verification_method": "runner_controlled_failure_path_v1",
            "path_name": path_name,
            "consumer_identity": consumer_identity,
            "independence_key": _canonical_json_sha256(consumer_identity),
            "hypothesis_id": control_verification.get("hypothesis_id"),
            "support_experiment_id": support_id,
            "support_selection_id": support_selection.get("selection_id"),
            "control_verification_id": control_verification_id,
            "mechanism_symbols": control_verification.get("mechanism_symbols"),
            "origin_atom_ids": origin_atom_ids,
            "observed_failure": {
                "source": observable.get("source") if isinstance(observable, dict) else None,
                "difference_kind": (
                    observable.get("difference_kind") if isinstance(observable, dict) else None
                ),
                **support_observation,
            },
        }
        path_receipt["failure_path_id"] = _content_addressed_receipt_id(
            "failure_path",
            path_receipt,
            "failure_path_id",
        )
        paths.append(path_receipt)
    paths.sort(key=lambda item: str(item.get("failure_path_id")))
    return paths


def _structured_argv_intervention_difference(
    *,
    baseline_replay: dict[str, Any],
    challenge_replay: dict[str, Any],
    planning_workspace: Path,
) -> dict[str, Any] | None:
    """Prove one changed repository-file input from runner-executed argv."""

    baseline_argv = baseline_replay.get("executed_argv")
    challenge_argv = challenge_replay.get("executed_argv")
    if (
        not isinstance(baseline_argv, list)
        or not isinstance(challenge_argv, list)
        or len(baseline_argv) != len(challenge_argv)
        or not baseline_argv
        or any(not isinstance(token, str) or not token for token in baseline_argv)
        or any(not isinstance(token, str) or not token for token in challenge_argv)
    ):
        return None
    changed = [
        index
        for index, (baseline, challenge) in enumerate(
            zip(baseline_argv, challenge_argv, strict=True)
        )
        if baseline != challenge
    ]
    if len(changed) != 1:
        return None
    index = changed[0]
    baseline_token = str(baseline_argv[index])
    challenge_token = str(challenge_argv[index])
    root = planning_workspace.resolve()
    baseline_path = (root / baseline_token).resolve()
    challenge_path = (root / challenge_token).resolve()
    if (
        not _within(baseline_path, root)
        or not _within(challenge_path, root)
        or not baseline_path.is_file()
        or not challenge_path.is_file()
        or baseline_path.is_symlink()
        or challenge_path.is_symlink()
        or baseline_path.suffix.casefold()
        not in {".json", ".toml", ".yaml", ".yml", ".txt", ".csv"}
        or challenge_path.suffix.casefold()
        not in {".json", ".toml", ".yaml", ".yml", ".txt", ".csv"}
    ):
        return None
    baseline_hash = _sha256_path(baseline_path)
    challenge_hash = _sha256_path(challenge_path)
    return {
        "verification_method": "executed_argv_repository_file_delta_v1",
        "difference_count": 1,
        "difference": {
            "slot": f"argv:{index}",
            "difference_kind": "repository_file_input_changed",
            "baseline_argument": PurePosixPath(baseline_token.replace("\\", "/")).as_posix(),
            "challenge_argument": PurePosixPath(challenge_token.replace("\\", "/")).as_posix(),
            "baseline_file_sha256": baseline_hash,
            "challenge_file_sha256": challenge_hash,
            "content_relation": (
                "same_content_different_identity"
                if baseline_hash == challenge_hash
                else "different_content"
            ),
        },
    }


def _retained_harness_scalar_argv_difference(
    *,
    baseline_replay: dict[str, Any],
    challenge_replay: dict[str, Any],
    mechanism_symbols: list[str],
    symbol_paths: dict[str, str],
) -> dict[str, Any] | None:
    """Prove one changed scalar argv value flows into the retained mechanism call."""

    baseline_argv = baseline_replay.get("executed_argv")
    challenge_argv = challenge_replay.get("executed_argv")
    baseline_harness = _research_harness_relative_path(baseline_argv)
    challenge_harness = _research_harness_relative_path(challenge_argv)
    if (
        baseline_harness is None
        or challenge_harness != baseline_harness
        or not isinstance(baseline_argv, list)
        or not isinstance(challenge_argv, list)
        or len(baseline_argv) != len(challenge_argv)
    ):
        return None
    changed = [
        index
        for index, (baseline, challenge) in enumerate(
            zip(baseline_argv, challenge_argv, strict=True)
        )
        if baseline != challenge
    ]
    if len(changed) != 1:
        return None
    changed_index = changed[0]
    normalized_harness = baseline_harness.replace("\\", "/")
    script_indices = [
        index
        for index, token in enumerate(baseline_argv)
        if isinstance(token, str) and token.replace("\\", "/") == normalized_harness
    ]
    if len(script_indices) != 1 or changed_index <= script_indices[0]:
        return None
    runtime_argv_index = changed_index - script_indices[0]
    baseline_value = baseline_argv[changed_index]
    challenge_value = challenge_argv[changed_index]
    if (
        not isinstance(baseline_value, str)
        or not isinstance(challenge_value, str)
        or not baseline_value
        or not challenge_value
        or len(baseline_value) > 256
        or len(challenge_value) > 256
        or any(character in baseline_value + challenge_value for character in ("/", "\\", "\n"))
    ):
        return None
    workspace_raw = _text(baseline_replay.get("workspace_dir"))
    if workspace_raw is None or workspace_raw != _text(challenge_replay.get("workspace_dir")):
        return None
    workspace = Path(workspace_raw).resolve()
    harness_path = (workspace / baseline_harness).resolve()
    if (
        not _within(harness_path, workspace)
        or not harness_path.is_file()
        or harness_path.is_symlink()
    ):
        return None
    try:
        content = harness_path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(content)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    aliases = _module_import_aliases(tree)

    def reads_runtime_argv(node: ast.AST, derived_names: set[str]) -> bool:
        for candidate in ast.walk(node):
            if isinstance(candidate, ast.Subscript):
                value = _dotted_expression(candidate.value)
                index_node = candidate.slice
                if (
                    value == "sys.argv"
                    and isinstance(index_node, ast.Constant)
                    and index_node.value == runtime_argv_index
                ):
                    return True
            if (
                isinstance(candidate, ast.Name)
                and isinstance(candidate.ctx, ast.Load)
                and candidate.id in derived_names
            ):
                return True
        return False

    derived_names: set[str] = set()
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    while True:
        added: set[str] = set()
        for assignment in assignments:
            value = assignment.value
            if value is None or not reads_runtime_argv(value, derived_names):
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            )
            added.update(
                target.id
                for target in targets
                for target in ast.walk(target)
                if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
            )
        if added.issubset(derived_names):
            break
        derived_names.update(added)

    bindings: list[dict[str, Any]] = []
    for symbol in mechanism_symbols:
        source_path = symbol_paths.get(symbol)
        if source_path is None:
            return None
        targets = _mechanism_call_targets(symbol=symbol, source_path=source_path)
        symbol_bindings: list[dict[str, Any]] = []
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            expression = _dotted_expression(call.func)
            resolved = _resolved_call_expression(expression, aliases) if expression else None
            if expression not in targets and resolved not in targets:
                continue
            values = [*call.args, *(keyword.value for keyword in call.keywords)]
            for argument_index, value in enumerate(values):
                if reads_runtime_argv(value, derived_names):
                    symbol_bindings.append(
                        {
                            "symbol": symbol,
                            "line": call.lineno,
                            "argument_index": argument_index,
                            "argument_ast_sha256": sha256(
                                ast.dump(
                                    value,
                                    annotate_fields=True,
                                    include_attributes=False,
                                ).encode("utf-8")
                            ).hexdigest(),
                        }
                    )
        if not symbol_bindings:
            return None
        bindings.extend(symbol_bindings)
    return {
        "verification_method": "retained_harness_scalar_argv_delta_v1",
        "difference_count": 1,
        "difference": {
            "slot": f"argv:{changed_index}",
            "difference_kind": "scalar_argument_changed",
            "runtime_argv_index": runtime_argv_index,
            "baseline_value_sha256": _canonical_json_sha256(baseline_value),
            "challenge_value_sha256": _canonical_json_sha256(challenge_value),
            "harness_path": baseline_harness,
            "harness_sha256": _sha256_path(harness_path),
            "mechanism_argument_bindings": bindings,
        },
    }


def _falsification_intervention_receipts(
    dossier: dict[str, Any],
    *,
    clean_replays: dict[str, dict[str, Any]],
    planning_workspace: Path,
    symbol_receipts: list[dict[str, str]],
    errors: list[str],
) -> list[dict[str, Any]]:
    """Prove that a falsification challenge actually changes one causal input.

    Model-authored labels such as "alternative removed" are not evidence.  For the
    supported pytest route, mint this receipt only when the baseline and challenge
    make one runner-inspected call to the same mechanism, differ in exactly one AST
    argument slot, and both clean replays establish the declared challenge polarity.
    """

    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    symbol_paths = {
        str(receipt.get("symbol")): str(receipt.get("path"))
        for receipt in symbol_receipts
        if _text(receipt.get("symbol")) is not None and _text(receipt.get("path")) is not None
    }
    receipts: list[dict[str, Any]] = []
    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = _text(hypothesis.get("hypothesis_id"))
        mechanism_raw = hypothesis.get("mechanism_symbols")
        mechanism_symbols = (
            [
                symbol.strip()
                for symbol in mechanism_raw
                if isinstance(symbol, str) and symbol.strip()
            ]
            if isinstance(mechanism_raw, list)
            else []
        )
        attempts_raw = hypothesis.get("falsification_attempts")
        attempts = attempts_raw if isinstance(attempts_raw, list) else []
        for attempt in attempts:
            if not isinstance(attempt, dict) or attempt.get("outcome") == "inconclusive":
                continue
            attempt_id = _text(attempt.get("attempt_id"))
            baseline_id = _text(attempt.get("baseline_experiment_id"))
            challenge_id = _text(attempt.get("challenge_experiment_id"))
            outcome = _text(attempt.get("outcome"))
            baseline = experiments.get(baseline_id or "")
            challenge = experiments.get(challenge_id or "")
            baseline_replay = clean_replays.get(baseline_id or "")
            challenge_replay = clean_replays.get(challenge_id or "")
            label = f"{hypothesis_id or 'unknown'}:{attempt_id or 'unknown'}"
            if (
                hypothesis_id is None
                or attempt_id is None
                or baseline_id is None
                or challenge_id is None
                or not mechanism_symbols
                or not isinstance(baseline, dict)
                or not isinstance(challenge, dict)
                or not isinstance(baseline_replay, dict)
                or not isinstance(challenge_replay, dict)
            ):
                errors.append(f"falsification_intervention_unresolved:{label}")
                continue
            relationship_raw = challenge.get("control_relationship")
            relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
            relationship_symbols = _declared_mechanism_subset(
                relationship.get("mechanism_symbols"),
                hypothesis_symbols=mechanism_symbols,
            )
            if (
                challenge.get("scenario_kind") != "control"
                or relationship.get("supports_experiment_id") != baseline_id
                or relationship_symbols is None
            ):
                errors.append(f"falsification_intervention_relationship_unbound:{label}")
                continue
            selection_errors: list[str] = []
            baseline_selection = _pytest_test_selection_receipt(
                hypothesis_id=hypothesis_id,
                experiment_id=baseline_id,
                experiment=baseline,
                replay=baseline_replay,
                mechanism_symbols=relationship_symbols,
                symbol_paths=symbol_paths,
                planning_workspace=planning_workspace,
                errors=selection_errors,
            )
            challenge_selection = _pytest_test_selection_receipt(
                hypothesis_id=hypothesis_id,
                experiment_id=challenge_id,
                experiment=challenge,
                replay=challenge_replay,
                mechanism_symbols=relationship_symbols,
                symbol_paths=symbol_paths,
                planning_workspace=planning_workspace,
                errors=selection_errors,
            )
            structural = (
                _structural_controlled_difference(
                    hypothesis_id=hypothesis_id,
                    control_id=challenge_id,
                    mechanism_symbols=relationship_symbols,
                    support_selection=baseline_selection,
                    control_selection=challenge_selection,
                    errors=selection_errors,
                )
                if isinstance(baseline_selection, dict) and isinstance(challenge_selection, dict)
                else None
            )
            verification_method = "pytest_ast_falsification_intervention_v1"
            mechanism_verification_mode: str | None = None
            baseline_selection_id = (
                baseline_selection.get("selection_id")
                if isinstance(baseline_selection, dict)
                else None
            )
            challenge_selection_id = (
                challenge_selection.get("selection_id")
                if isinstance(challenge_selection, dict)
                else None
            )
            if structural is None:
                structural = _structured_argv_intervention_difference(
                    baseline_replay=baseline_replay,
                    challenge_replay=challenge_replay,
                    planning_workspace=planning_workspace,
                )
                if structural is None:
                    structural = _retained_harness_scalar_argv_difference(
                        baseline_replay=baseline_replay,
                        challenge_replay=challenge_replay,
                        mechanism_symbols=relationship_symbols,
                        symbol_paths=symbol_paths,
                    )
                if structural is not None:
                    baseline_modes = _non_pytest_mechanism_proof_modes(
                        experiment=baseline,
                        replay=baseline_replay,
                        mechanism_symbols=relationship_symbols,
                        symbol_paths=symbol_paths,
                    )
                    challenge_modes = _non_pytest_mechanism_proof_modes(
                        experiment=challenge,
                        replay=challenge_replay,
                        mechanism_symbols=relationship_symbols,
                        symbol_paths=symbol_paths,
                    )
                    shared_modes = sorted(baseline_modes & challenge_modes)
                    if not shared_modes:
                        reason = (
                            "mode_switch"
                            if baseline_modes and challenge_modes
                            else "shared_mechanism_missing"
                        )
                        errors.append(f"falsification_intervention_{reason}:{label}")
                        structural = None
                    else:
                        verification_method = "runner_argv_falsification_intervention_v2"
                        mechanism_verification_mode = shared_modes[0]
                        baseline_selection_id = "argv_selection:" + _canonical_json_sha256(
                            baseline_replay.get("executed_argv")
                        )
                        challenge_selection_id = "argv_selection:" + _canonical_json_sha256(
                            challenge_replay.get("executed_argv")
                        )
            else:
                baseline_verified = _selection_verified_symbols(
                    baseline_selection,
                    requested_symbols=relationship_symbols,
                )
                challenge_verified = _selection_verified_symbols(
                    challenge_selection,
                    requested_symbols=relationship_symbols,
                )
                if (
                    baseline_verified != relationship_symbols
                    or challenge_verified != relationship_symbols
                ):
                    errors.append(f"falsification_intervention_shared_mechanism_missing:{label}")
                    structural = None
                else:
                    mechanism_verification_mode = "pytest_ast_selection"
            disproof = attempt.get("disproof_condition")
            challenge_assertion = challenge.get("observable_assertion")
            assertions_verified = (
                baseline_replay.get("assertion_passed") is True
                and challenge_replay.get("assertion_passed") is True
                and isinstance(disproof, dict)
                and isinstance(challenge_assertion, dict)
                and _falsification_assertion_relation(
                    disproof,
                    challenge_assertion,
                    outcome=outcome or "",
                )
            )
            if structural is None or not assertions_verified:
                detail = ",".join(dict.fromkeys(selection_errors)) or "causal_delta_missing"
                errors.append(f"falsification_intervention_unverified:{label}:{detail}")
                continue
            observation = {
                "verification_method": "runner_replay_falsification_polarity_v1",
                "polarity": (
                    "failure_persists_after_intervention"
                    if outcome == "survived"
                    else "disproof_observed_after_intervention"
                ),
                "baseline": {
                    "exit_code": baseline_replay.get("exit_code"),
                    "stdout_sha256": baseline_replay.get("stdout_sha256"),
                    "stderr_sha256": baseline_replay.get("stderr_sha256"),
                },
                "challenge": {
                    "exit_code": challenge_replay.get("exit_code"),
                    "stdout_sha256": challenge_replay.get("stdout_sha256"),
                    "stderr_sha256": challenge_replay.get("stderr_sha256"),
                },
            }
            receipt: dict[str, Any] = {
                "verification_method": verification_method,
                "hypothesis_id": hypothesis_id,
                "attempt_id": attempt_id,
                "baseline_experiment_id": baseline_id,
                "challenge_experiment_id": challenge_id,
                "mechanism_symbols": relationship_symbols,
                "baseline_verified_mechanism_symbols": relationship_symbols,
                "challenge_verified_mechanism_symbols": relationship_symbols,
                "shared_verified_mechanism_symbols": relationship_symbols,
                "mechanism_verification_mode": mechanism_verification_mode,
                "baseline_selection_id": baseline_selection_id,
                "challenge_selection_id": challenge_selection_id,
                "controlled_input_difference": structural,
                "observed_polarity": observation,
                "relationship_sha256": _canonical_json_sha256(
                    {
                        "controlled_variable": relationship.get("controlled_variable"),
                        "expected_difference": relationship.get("expected_difference"),
                        "mechanism_symbols": relationship_symbols,
                    }
                ),
            }
            receipt["intervention_receipt_id"] = _content_addressed_receipt_id(
                "falsification_intervention",
                receipt,
                "intervention_receipt_id",
            )
            receipts.append(receipt)
    receipts.sort(key=lambda item: str(item.get("intervention_receipt_id")))
    return receipts


def _causal_control_receipts(
    dossier: dict[str, Any],
    *,
    clean_replays: dict[str, dict[str, Any]],
    planning_workspace: Path,
    symbol_receipts: list[dict[str, str]],
    errors: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prove one controlled input causes a complementary replay result."""
    symbol_paths = {
        str(receipt.get("symbol")): str(receipt.get("path"))
        for receipt in symbol_receipts
        if _text(receipt.get("symbol")) is not None and _text(receipt.get("path")) is not None
    }
    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    selections_by_id: dict[str, dict[str, Any]] = {}
    controls: list[dict[str, Any]] = []
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = _text(hypothesis.get("hypothesis_id"))
        symbols_raw = hypothesis.get("mechanism_symbols")
        mechanism_symbols = (
            [symbol.strip() for symbol in symbols_raw if isinstance(symbol, str) and symbol.strip()]
            if isinstance(symbols_raw, list)
            else []
        )
        counter_raw = hypothesis.get("counterevidence")
        counter_ids = counter_raw if isinstance(counter_raw, list) else []
        if hypothesis_id is None:
            continue
        for raw_control_id in counter_ids:
            control_id = _text(raw_control_id)
            control = experiments.get(control_id or "")
            if (
                control_id is None
                or not isinstance(control, dict)
                or control.get("scenario_kind") != "control"
                or control.get("outcome") != "refutes"
            ):
                continue
            relationship_raw = control.get("control_relationship")
            relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
            relationship_symbols = _declared_mechanism_subset(
                relationship.get("mechanism_symbols"),
                hypothesis_symbols=mechanism_symbols,
            )
            if relationship_symbols is None:
                errors.append(
                    f"causal_control_mechanism_subset_invalid:{hypothesis_id}:{control_id}"
                )
                continue
            support_id = _text(relationship.get("supports_experiment_id"))
            support = experiments.get(support_id or "")
            if support_id is None or not isinstance(support, dict):
                errors.append(f"causal_control_support_unresolved:{hypothesis_id}:{control_id}")
                continue
            pair_selections: dict[str, dict[str, Any]] = {}
            for role, experiment_id, experiment in (
                ("support", support_id, support),
                ("control", control_id, control),
            ):
                replay = clean_replays.get(experiment_id)
                if not isinstance(replay, dict):
                    errors.append(
                        f"causal_control_replay_missing:{hypothesis_id}:{experiment_id}:{role}"
                    )
                    continue
                selection = _pytest_test_selection_receipt(
                    hypothesis_id=hypothesis_id,
                    experiment_id=experiment_id,
                    experiment=experiment,
                    replay=replay,
                    mechanism_symbols=relationship_symbols,
                    symbol_paths=symbol_paths,
                    planning_workspace=planning_workspace,
                    errors=errors,
                )
                if selection is None:
                    continue
                selection_id = str(selection["selection_id"])
                previous = selections_by_id.get(selection_id)
                if previous is not None and previous != selection:
                    errors.append(f"causal_control_test_selection_conflict:{selection_id}")
                    continue
                selections_by_id[selection_id] = selection
                pair_selections[role] = selection
            support_selection = pair_selections.get("support")
            control_selection = pair_selections.get("control")
            if support_selection is None or control_selection is None:
                continue
            if support_selection.get("test_path") == control_selection.get(
                "test_path"
            ) and support_selection.get("selector") == control_selection.get("selector"):
                errors.append(f"causal_control_same_test_selection:{hypothesis_id}:{control_id}")
                continue
            support_symbols = {
                touch.get("symbol")
                for touch in support_selection.get("mechanism_touches", [])
                if isinstance(touch, dict)
            }
            control_symbols = {
                touch.get("symbol")
                for touch in control_selection.get("mechanism_touches", [])
                if isinstance(touch, dict)
            }
            shared_symbols = [
                symbol
                for symbol in relationship_symbols
                if symbol in support_symbols and symbol in control_symbols
            ]
            if shared_symbols != relationship_symbols:
                errors.append(
                    f"causal_control_mechanism_coverage_missing:{hypothesis_id}:{control_id}"
                )
                continue
            structural_difference = _structural_controlled_difference(
                hypothesis_id=hypothesis_id,
                control_id=control_id,
                mechanism_symbols=shared_symbols,
                support_selection=support_selection,
                control_selection=control_selection,
                errors=errors,
            )
            support_replay = clean_replays.get(support_id)
            control_replay = clean_replays.get(control_id)
            observable_difference = (
                _observable_controlled_difference(
                    hypothesis_id=hypothesis_id,
                    control_id=control_id,
                    support=support,
                    control=control,
                    support_replay=support_replay,
                    control_replay=control_replay,
                    errors=errors,
                )
                if isinstance(support_replay, dict) and isinstance(control_replay, dict)
                else None
            )
            if structural_difference is None or observable_difference is None:
                continue
            relationship_projection = {
                "controlled_variable": relationship.get("controlled_variable"),
                "expected_difference": relationship.get("expected_difference"),
                "mechanism_symbols": relationship_symbols,
            }
            control_receipt: dict[str, Any] = {
                "verification_method": "pytest_ast_controlled_difference_v2",
                "hypothesis_id": hypothesis_id,
                "support_experiment_id": support_id,
                "control_experiment_id": control_id,
                "support_selection_id": support_selection["selection_id"],
                "control_selection_id": control_selection["selection_id"],
                "mechanism_symbols": shared_symbols,
                "support_verified_mechanism_symbols": shared_symbols,
                "control_verified_mechanism_symbols": shared_symbols,
                "shared_verified_mechanism_symbols": shared_symbols,
                "mechanism_verification_mode": "pytest_ast_selection",
                "same_test_file": support_selection.get("test_path")
                == control_selection.get("test_path"),
                "controlled_input_difference": structural_difference,
                "observable_difference": observable_difference,
                "adversarial_effect": "limits_scope",
                "relationship_sha256": _canonical_json_sha256(relationship_projection),
            }
            control_receipt["control_verification_id"] = _content_addressed_receipt_id(
                "control_verification",
                control_receipt,
                "control_verification_id",
            )
            controls.append(control_receipt)
    selections = sorted(
        selections_by_id.values(),
        key=lambda item: (str(item.get("hypothesis_id")), str(item.get("experiment_id"))),
    )
    controls.sort(
        key=lambda item: (
            str(item.get("hypothesis_id")),
            str(item.get("control_experiment_id")),
        )
    )
    return selections, controls


def _deterministic_mechanism_closure_receipts(
    dossier: dict[str, Any],
    *,
    clean_replays: dict[str, dict[str, Any]],
    symbol_receipts: list[dict[str, str]],
    mechanism_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mint a non-counterfactual closure only for complete deterministic evidence.

    Some static/config mechanisms have no honest alternative input to toggle.  They
    may advance without invented falsification only when runner evidence closes the
    full declared mechanism path, all real alternatives are refuted, and no material
    root-cause unknown remains.
    """

    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    if not hypotheses or not isinstance(hypotheses[0], dict):
        return []
    primary = hypotheses[0]
    if primary.get("falsification_attempts"):
        return []
    if any(
        isinstance(unknown, dict)
        and "root_cause"
        in (unknown.get("affects") if isinstance(unknown.get("affects"), list) else [])
        for unknown in (
            dossier.get("material_unknowns")
            if isinstance(dossier.get("material_unknowns"), list)
            else []
        )
    ):
        return []
    alternatives = [value for value in hypotheses[1:] if isinstance(value, dict)]
    if any(value.get("disposition") != "refuted" for value in alternatives):
        return []
    hypothesis_id = _text(primary.get("hypothesis_id"))
    mechanism_raw = primary.get("mechanism_symbols")
    mechanism_symbols = (
        [symbol.strip() for symbol in mechanism_raw if isinstance(symbol, str) and symbol.strip()]
        if isinstance(mechanism_raw, list)
        else []
    )
    symbol_paths = {
        str(receipt.get("symbol")): str(receipt.get("path"))
        for receipt in symbol_receipts
        if _text(receipt.get("symbol")) is not None and _text(receipt.get("path")) is not None
    }
    if (
        hypothesis_id is None
        or not mechanism_symbols
        or len(set(mechanism_symbols)) != len(mechanism_symbols)
    ):
        return []
    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    candidate_evidence = [
        value
        for value in mechanism_evidence
        if isinstance(value, dict)
        and value.get("hypothesis_id") == hypothesis_id
        and value.get("adversarial_effect") == "supports_selection"
    ]
    connected, covered_symbols, support_connectivity, disconnected = _rooted_support_connectivity(
        candidate_evidence,
        hypothesis_symbols=mechanism_symbols,
    )
    if disconnected or covered_symbols != set(mechanism_symbols) or not connected:
        return []
    support_ids = sorted(
        {
            experiment_id
            for evidence in connected
            for experiment_id in evidence.get("experiment_ids", [])
            if isinstance(experiment_id, str)
        }
    )
    declared_support_ids = {
        value for value in primary.get("supporting_evidence", []) if isinstance(value, str)
    }
    if not support_ids or not set(support_ids).issubset(declared_support_ids):
        return []
    observed_results: list[dict[str, Any]] = []
    origin_atom_ids: set[str] = set()
    for support_id in support_ids:
        experiment = experiments.get(support_id)
        replay = clean_replays.get(support_id)
        if (
            not isinstance(experiment, dict)
            or not isinstance(replay, dict)
            or experiment.get("outcome") != "supports"
            or replay.get("assertion_passed") is not True
        ):
            return []
        origin_atom_ids.update(
            value for value in experiment.get("addresses_atom_ids", []) if isinstance(value, str)
        )
        observed_results.append(
            {
                "experiment_id": support_id,
                "scenario_kind": experiment.get("scenario_kind"),
                "exit_code": replay.get("exit_code"),
                "stdout_sha256": replay.get("stdout_sha256"),
                "stderr_sha256": replay.get("stderr_sha256"),
                "assertion": experiment.get("observable_assertion"),
            }
        )
    code_path = [
        {"symbol": symbol, "path": symbol_paths[symbol]}
        for symbol in mechanism_symbols
        if symbol in symbol_paths
    ]
    if len(code_path) != len(mechanism_symbols):
        return []
    receipt: dict[str, Any] = {
        "verification_method": "runner_deterministic_mechanism_closure_v2",
        "hypothesis_id": hypothesis_id,
        "support_experiment_ids": support_ids,
        "mechanism_evidence_ids": sorted(
            str(value["mechanism_evidence_id"]) for value in connected
        ),
        "causal_root_evidence_ids": sorted(
            str(value["mechanism_evidence_id"])
            for value in connected
            if value.get("causal_root_bindings")
        ),
        "mechanism_symbols": mechanism_symbols,
        "code_path": code_path,
        "closure_basis": "rooted_connected_support_component",
        "support_connectivity": support_connectivity,
        "alternatives_disposed": [str(value.get("hypothesis_id")) for value in alternatives],
        "origin_atom_ids": sorted(origin_atom_ids),
        "observed_results": observed_results,
    }
    receipt["closure_receipt_id"] = _content_addressed_receipt_id(
        "deterministic_mechanism_closure",
        receipt,
        "closure_receipt_id",
    )
    return [receipt]


def _runner_observed_root_mechanism_symbol(
    *,
    mechanism_symbols: list[str],
    mechanism_link: dict[str, Any] | None,
) -> str | None:
    """Select one runner-observed production symbol as the causal graph root."""

    if not isinstance(mechanism_link, dict):
        return None
    verification_method = _text(mechanism_link.get("verification_method"))
    if verification_method is None or not verification_method.startswith("runner_"):
        return None
    declared = set(mechanism_symbols)
    entrypoint = _text(mechanism_link.get("entrypoint"))
    if entrypoint in declared:
        return entrypoint
    if verification_method == "runner_python_call_chain_v1":
        code_path_raw = mechanism_link.get("code_path")
        code_path = code_path_raw if isinstance(code_path_raw, list) else []
        path_symbols = [
            str(item.get("symbol"))
            for item in code_path
            if isinstance(item, dict) and _text(item.get("symbol")) in declared
        ]
        if path_symbols:
            return path_symbols[0]
    sinks_raw = mechanism_link.get("symbol_sinks")
    sink_symbols = sorted(
        {
            str(item.get("symbol"))
            for item in (sinks_raw if isinstance(sinks_raw, list) else [])
            if isinstance(item, dict) and _text(item.get("symbol")) in declared
        }
    )
    return sink_symbols[0] if len(sink_symbols) == 1 else None


def _derived_causal_root_bindings(
    *,
    experiment_ids: list[str],
    origin_atom_ids: list[str],
    origin_symptom_bindings: list[dict[str, Any]],
    mechanism_symbols: list[str],
    mechanism_link: dict[str, Any] | None,
    executed_argv: list[str] | None,
    command_authorization: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Derive roots only from runner-attested symptom or immutable-command facts."""

    roots: list[dict[str, Any]] = []
    experiment_set = set(experiment_ids)
    origin_atom_set = set(origin_atom_ids)
    link_method = (
        _text(mechanism_link.get("verification_method"))
        if isinstance(mechanism_link, dict)
        else None
    )
    link_entrypoint = (
        _text(mechanism_link.get("entrypoint")) if isinstance(mechanism_link, dict) else None
    )
    root_mechanism_symbol = _runner_observed_root_mechanism_symbol(
        mechanism_symbols=mechanism_symbols,
        mechanism_link=mechanism_link,
    )
    valid_symptom_bindings: list[dict[str, Any]] = []
    symptom_match_kinds = {
        "command_and_exit_code",
        "command_and_atom_evidence_symptom",
        "faithful_atom_evidence_symptom",
        "command_and_artifact_symptom_text",
        "faithful_artifact_symptom_text",
        "explicit_symptom_field_binding",
    }
    for binding in origin_symptom_bindings:
        experiment_id = _text(binding.get("experiment_id")) or _text(
            binding.get("baseline_experiment_id")
        )
        atom_id = _text(binding.get("atom_id"))
        match_kind = _text(binding.get("match_kind"))
        atom_sha256 = _text(binding.get("origin_atom_sha256"))
        predicate_binding = bool(
            binding.get("runner_attested") is True
            and binding.get("atom_field_binding_sha256")
            == _canonical_json_sha256(
                {
                    key: value
                    for key, value in binding.items()
                    if key != "atom_field_binding_sha256"
                }
            )
            and isinstance(binding.get("observation_predicate"), Mapping)
            and not proof_predicate_contract_errors(binding.get("observation_predicate"))
        )
        if (
            experiment_id not in experiment_set
            or atom_id not in origin_atom_set
            or (match_kind not in symptom_match_kinds and not predicate_binding)
            or atom_sha256 is None
            or re.fullmatch(r"[0-9a-f]{64}", atom_sha256) is None
        ):
            continue
        valid_symptom_bindings.append(dict(binding))
    valid_symptom_bindings.sort(
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
    )
    if (
        valid_symptom_bindings
        and link_method is not None
        and link_method.startswith("runner_")
        and link_entrypoint is not None
        and root_mechanism_symbol is not None
        and isinstance(mechanism_link, dict)
    ):
        roots.append(
            {
                "kind": "origin_symptom_observation",
                "experiment_ids": sorted(experiment_set),
                "origin_atom_ids": sorted(
                    {
                        str(binding["atom_id"])
                        for binding in valid_symptom_bindings
                        if isinstance(binding.get("atom_id"), str)
                    }
                ),
                "origin_bindings_sha256": _canonical_json_sha256(valid_symptom_bindings),
                "mechanism_link_sha256": _canonical_json_sha256(mechanism_link),
                "root_mechanism_symbol": root_mechanism_symbol,
            }
        )

    authorization = command_authorization if isinstance(command_authorization, dict) else {}
    argv = executed_argv if isinstance(executed_argv, list) else []
    origin_atom_id = _text(authorization.get("origin_atom_id"))
    origin_atom_sha256 = _text(authorization.get("origin_atom_sha256"))
    origin_command_sha256 = _text(authorization.get("origin_command_value_sha256"))
    if (
        argv
        and all(isinstance(token, str) and token for token in argv)
        and _command_authorization_attested(authorization, argv=argv)
        and origin_atom_id in origin_atom_set
        and authorization.get("origin_atom_field_path") == "$.command"
        and origin_atom_sha256 is not None
        and re.fullmatch(r"[0-9a-f]{64}", origin_atom_sha256) is not None
        and origin_command_sha256 is not None
        and re.fullmatch(r"[0-9a-f]{64}", origin_command_sha256) is not None
        and root_mechanism_symbol is not None
    ):
        roots.append(
            {
                "kind": "immutable_source_command",
                "experiment_ids": sorted(experiment_set),
                "origin_atom_ids": [origin_atom_id],
                "origin_atom_sha256": origin_atom_sha256,
                "origin_atom_field_path": "$.command",
                "origin_command_value_sha256": origin_command_sha256,
                "executed_argv_sha256": authorization["executed_argv_sha256"],
                "root_mechanism_symbol": root_mechanism_symbol,
            }
        )
    return sorted(
        roots,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )


def _runner_verified_support_edges(
    supports: list[dict[str, Any]],
    *,
    hypothesis_symbols: list[str],
) -> list[dict[str, Any]]:
    """Return only source/AST-attested call edges already minted by the runner."""

    allowed_symbols = set(hypothesis_symbols)
    edges: dict[str, dict[str, Any]] = {}
    for support in supports:
        source_evidence_id = _text(support.get("mechanism_evidence_id"))
        if source_evidence_id is None:
            continue
        link_raw = support.get("mechanism_link")
        link = link_raw if isinstance(link_raw, dict) else {}
        supplied_link_hash = _text(link.get("mechanism_link_sha256"))
        link_projection = {
            key: value for key, value in link.items() if key != "mechanism_link_sha256"
        }
        if supplied_link_hash != _canonical_json_sha256(link_projection):
            continue
        code_path_raw = link.get("code_path")
        code_path = code_path_raw if isinstance(code_path_raw, list) else []
        verified_points = {
            (str(point.get("symbol")), str(point.get("path")))
            for point in code_path
            if isinstance(point, dict)
            and _text(point.get("symbol")) is not None
            and _text(point.get("path")) is not None
        }
        directed_edges = link.get("verified_directed_edges")
        for raw_edge in directed_edges if isinstance(directed_edges, list) else []:
            if not isinstance(raw_edge, dict):
                continue
            caller_symbol = _text(raw_edge.get("from_locator"))
            callee_symbol = _text(raw_edge.get("to_locator"))
            evidence_sha256 = _text(raw_edge.get("evidence_sha256"))
            if (
                caller_symbol not in allowed_symbols
                or callee_symbol not in allowed_symbols
                or evidence_sha256 is None
                or re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is None
                or raw_edge.get("runner_attested") is not True
            ):
                continue
            edge = {
                "caller_symbol": caller_symbol,
                "caller_path": caller_symbol,
                "callee_symbol": callee_symbol,
                "callee_path": callee_symbol,
                "edge_kind": raw_edge.get("kind"),
                "edge_evidence_sha256": evidence_sha256,
                "mechanism_link_sha256": supplied_link_hash,
                "source_mechanism_evidence_id": source_evidence_id,
            }
            edge["causal_edge_sha256"] = _canonical_json_sha256(edge)
            edges[str(edge["causal_edge_sha256"])] = edge
        raw_edges = link.get("verified_call_edges")
        for raw_edge in raw_edges if isinstance(raw_edges, list) else []:
            if not isinstance(raw_edge, dict):
                continue
            caller_symbol = _text(raw_edge.get("caller_symbol"))
            caller_path = _text(raw_edge.get("caller_path"))
            callee_symbol = _text(raw_edge.get("callee_symbol"))
            callee_path = _text(raw_edge.get("callee_path"))
            resolved_call = _text(raw_edge.get("resolved_call"))
            call_ast_sha256 = _text(raw_edge.get("call_ast_sha256"))
            line = raw_edge.get("line")
            if (
                caller_symbol not in allowed_symbols
                or callee_symbol not in allowed_symbols
                or caller_path is None
                or callee_path is None
                or (caller_symbol, caller_path) not in verified_points
                or (callee_symbol, callee_path) not in verified_points
                or resolved_call is None
                or not isinstance(line, int)
                or isinstance(line, bool)
                or line < 1
                or call_ast_sha256 is None
                or re.fullmatch(r"[0-9a-f]{64}", call_ast_sha256) is None
            ):
                continue
            edge = {
                "caller_symbol": caller_symbol,
                "caller_path": caller_path,
                "callee_symbol": callee_symbol,
                "callee_path": callee_path,
                "line": line,
                "resolved_call": resolved_call,
                "call_ast_sha256": call_ast_sha256,
                "mechanism_link_sha256": supplied_link_hash,
                "source_mechanism_evidence_id": source_evidence_id,
            }
            edge["causal_edge_sha256"] = _canonical_json_sha256(edge)
            edges[str(edge["causal_edge_sha256"])] = edge
    return [edges[key] for key in sorted(edges)]


def _rooted_support_connectivity(
    supports: list[dict[str, Any]],
    *,
    hypothesis_symbols: list[str],
) -> tuple[list[dict[str, Any]], set[str], list[dict[str, Any]], list[str]]:
    """Connect support receipts to an immutable symptom/command evidence root.

    Exactly one runner-observed mechanism symbol seeds a candidate component.
    Shared symbols may attach redundant evidence, but they never expand coverage.
    A receipt introduces every new symbol through its own forward AST-attested
    caller-to-callee edges.  The root whose component covers the most hypothesis
    symbols wins; ties prefer more connected receipts and then the lexically first
    evidence ID.  This prevents model-authored pair probes from manufacturing a
    causal chain while preserving aggregate evidence across real production edges.
    """

    records: dict[str, dict[str, Any]] = {}
    record_symbols: dict[str, set[str]] = {}
    for support in supports:
        evidence_id = _text(support.get("mechanism_evidence_id"))
        item_symbols = _declared_mechanism_subset(
            support.get("mechanism_symbols"),
            hypothesis_symbols=hypothesis_symbols,
        )
        if evidence_id is None or item_symbols is None or evidence_id in records:
            continue
        records[evidence_id] = support
        record_symbols[evidence_id] = set(item_symbols)
    edges = _runner_verified_support_edges(
        list(records.values()),
        hypothesis_symbols=hypothesis_symbols,
    )
    edges_by_source: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        edges_by_source.setdefault(str(edge["source_mechanism_evidence_id"]), []).append(edge)

    def receipt_forward_closure(
        evidence_id: str,
        *,
        starting_symbols: set[str],
        externally_reachable: set[str],
    ) -> tuple[set[str], list[dict[str, Any]]]:
        support_symbols = record_symbols[evidence_id]
        closure = support_symbols & starting_symbols
        used_edges: list[dict[str, Any]] = []
        remaining_edges = list(edges_by_source.get(evidence_id, []))
        while remaining_edges:
            progressed = False
            for edge in list(remaining_edges):
                caller = str(edge["caller_symbol"])
                callee = str(edge["callee_symbol"])
                if callee not in support_symbols or callee in closure:
                    remaining_edges.remove(edge)
                    continue
                if caller not in externally_reachable and caller not in closure:
                    continue
                closure.add(callee)
                used_edges.append(edge)
                remaining_edges.remove(edge)
                progressed = True
            if not progressed:
                break
        return closure, used_edges

    root_ids = [
        evidence_id
        for evidence_id in sorted(records)
        if isinstance(records[evidence_id].get("causal_root_bindings"), list)
        and records[evidence_id]["causal_root_bindings"]
    ]

    def expand_from_root(
        root_id: str,
    ) -> tuple[set[str], set[str], dict[str, dict[str, Any]]]:
        root = records[root_id]
        roots = root["causal_root_bindings"]
        root_symbols = {
            str(binding.get("root_mechanism_symbol"))
            for binding in roots
            if isinstance(binding, dict)
            and _text(binding.get("root_mechanism_symbol")) in record_symbols[root_id]
        }
        if len(root_symbols) != 1:
            return set(), set(), {}
        root_symbol = next(iter(root_symbols))
        root_reachable, root_edges = receipt_forward_closure(
            root_id,
            starting_symbols={root_symbol},
            externally_reachable=set(),
        )
        if root_reachable != record_symbols[root_id]:
            return set(), set(), {}
        connected = {root_id}
        reachable_symbols = set(root_reachable)
        trace_by_id: dict[str, dict[str, Any]] = {
            root_id: {
                "mechanism_evidence_id": root_id,
                "experiment_ids": sorted(
                    value for value in root.get("experiment_ids", []) if isinstance(value, str)
                ),
                "connection_kind": "causal_root",
                "connected_from_mechanism_evidence_id": None,
                "shared_verified_symbols": [],
                "verified_causal_edge": None,
                "verified_causal_edges": root_edges,
                "causal_root_kinds": sorted(
                    {
                        str(binding.get("kind"))
                        for binding in roots
                        if isinstance(binding, dict) and _text(binding.get("kind")) is not None
                    }
                ),
            }
        }
        remaining = set(records) - connected
        while remaining:
            progressed = False
            for evidence_id in sorted(remaining):
                support = records[evidence_id]
                support_symbols = record_symbols[evidence_id]
                shared_candidates: list[tuple[str, list[str]]] = []
                for connected_id in sorted(connected):
                    shared = sorted(support_symbols & record_symbols[connected_id])
                    if shared:
                        shared_candidates.append((connected_id, shared))
                predecessor_id: str | None = None
                shared_symbols: list[str] = []
                causal_edge: dict[str, Any] | None = None
                causal_edges: list[dict[str, Any]] = []
                connection_kind: str | None = None
                if support_symbols.issubset(reachable_symbols) and shared_candidates:
                    predecessor_id, shared_symbols = shared_candidates[0]
                    connection_kind = "shared_verified_symbol"
                else:
                    receipt_reachable, causal_edges = receipt_forward_closure(
                        evidence_id,
                        starting_symbols=support_symbols & reachable_symbols,
                        externally_reachable=reachable_symbols,
                    )
                    if receipt_reachable == support_symbols and causal_edges:
                        boundary_edge = next(
                            (
                                edge
                                for edge in causal_edges
                                if str(edge["caller_symbol"]) in reachable_symbols
                            ),
                            None,
                        )
                        caller = (
                            str(boundary_edge["caller_symbol"])
                            if isinstance(boundary_edge, dict)
                            else None
                        )
                        predecessor_ids = [
                            candidate_id
                            for candidate_id in sorted(connected)
                            if caller in record_symbols[candidate_id]
                        ]
                        if predecessor_ids and boundary_edge is not None:
                            predecessor_id = predecessor_ids[0]
                            causal_edge = boundary_edge
                        connection_kind = "runner_verified_causal_edge"
                if predecessor_id is None or connection_kind is None:
                    continue
                connected.add(evidence_id)
                remaining.remove(evidence_id)
                reachable_symbols.update(support_symbols)
                trace_by_id[evidence_id] = {
                    "mechanism_evidence_id": evidence_id,
                    "experiment_ids": sorted(
                        value
                        for value in support.get("experiment_ids", [])
                        if isinstance(value, str)
                    ),
                    "connection_kind": connection_kind,
                    "connected_from_mechanism_evidence_id": predecessor_id,
                    "shared_verified_symbols": shared_symbols,
                    "verified_causal_edge": causal_edge,
                    "verified_causal_edges": causal_edges,
                    "causal_root_kinds": [],
                }
                progressed = True
            if not progressed:
                break
        return connected, reachable_symbols, trace_by_id

    best_connected: set[str] = set()
    best_symbols: set[str] = set()
    best_trace: dict[str, dict[str, Any]] = {}
    best_score = (-1, -1)
    for root_id in root_ids:
        connected, reachable_symbols, trace = expand_from_root(root_id)
        score = (len(reachable_symbols), len(connected))
        if score > best_score:
            best_connected = connected
            best_symbols = reachable_symbols
            best_trace = trace
            best_score = score

    connected_supports = [records[evidence_id] for evidence_id in sorted(best_connected)]
    support_connectivity = [best_trace[evidence_id] for evidence_id in sorted(best_trace)]
    disconnected = sorted(set(records) - best_connected)
    return connected_supports, best_symbols, support_connectivity, disconnected


def _adapter_executed_consumer_receipt(
    proof: Mapping[str, Any],
    *,
    clean_replays: Mapping[str, Mapping[str, Any]],
    implementation_touchpoints: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Bind one adapter proof to an executed repository consumer and change surface.

    Intervention values are intentionally excluded from the consumer identity.  Two
    observations of the same tracked entrypoint/change surface therefore collapse to
    one breadth key, while independently executed entrypoints remain distinct even
    when they consume the same causal target.
    """

    observations = proof.get("observations")
    experiment_ids = [
        _text(observation.get("experiment_id"))
        for observation in (
            observations.get("baseline"),
            observations.get("challenge"),
        )
        if isinstance(observations, Mapping) and isinstance(observation, Mapping)
    ]
    if len(experiment_ids) != 2 or any(experiment_id is None for experiment_id in experiment_ids):
        return None

    authorization_identity: dict[str, Any] | None = None
    invocations: list[dict[str, Any]] = []
    for experiment_id in experiment_ids:
        replay = clean_replays.get(str(experiment_id))
        argv = replay.get("executed_argv") if isinstance(replay, Mapping) else None
        authorization = (
            replay.get("command_authorization") if isinstance(replay, Mapping) else None
        )
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(argument, str) or not argument for argument in argv)
            or not isinstance(authorization, Mapping)
            or command_authorization_errors(authorization, argv=argv)
            or authorization.get("authorization_kind") == "attested_research_harness"
        ):
            return None
        current_identity = command_authorization_identity(authorization)
        entrypoint_path = _text(authorization.get("entrypoint_path"))
        if (
            not isinstance(current_identity, dict)
            or (
                entrypoint_path is not None
                and entrypoint_path.replace("\\", "/").startswith(".usertest_research/")
            )
        ):
            return None
        if authorization_identity is None:
            authorization_identity = current_identity
        elif current_identity != authorization_identity:
            return None
        invocations.append(
            {
                "experiment_id": experiment_id,
                "executed_argv_sha256": authorization.get("executed_argv_sha256"),
                "command_authorization_sha256": authorization.get("authorization_sha256"),
            }
        )

    change_surfaces = sorted(
        [
            {
                "path": str(touchpoint.get("path")).replace("\\", "/"),
                "symbols": sorted(
                    str(symbol)
                    for symbol in (
                        touchpoint.get("symbols")
                        if isinstance(touchpoint.get("symbols"), list)
                        else []
                    )
                ),
                "inspected_content_sha256": touchpoint.get("inspected_content_sha256"),
            }
            for touchpoint in implementation_touchpoints
            if isinstance(touchpoint, Mapping)
            and _text(touchpoint.get("touchpoint_id")) is not None
            and _text(touchpoint.get("path")) is not None
            and touchpoint.get("runner_attested") is True
        ],
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    if authorization_identity is None or not change_surfaces:
        return None
    entrypoint = _text(authorization_identity.get("entrypoint_path")) or (
        "command_authorization:" + _canonical_json_sha256(authorization_identity)
    )
    consumer_identity = content_bound_payload(
        {
            "kind": "runner_observed_repository_consumer",
            "entrypoint": entrypoint,
            "command_authorization_identity": authorization_identity,
            "change_surfaces": change_surfaces,
            "attestation_basis": "executed_entrypoint_and_inspected_change_surface",
            "runner_attested": True,
        },
        hash_field="consumer_identity_sha256",
    )
    intervention = proof.get("intervention")
    causal_target = (
        _text(intervention.get("target")) if isinstance(intervention, Mapping) else None
    )
    return content_bound_payload(
        {
            "verification_method": "runner_adapter_consumer_binding_v1",
            "consumer_identity": consumer_identity,
            "invocations": sorted(invocations, key=lambda value: str(value["experiment_id"])),
            "implementation_touchpoint_ids": sorted(
                str(touchpoint["touchpoint_id"])
                for touchpoint in implementation_touchpoints
                if isinstance(touchpoint, Mapping)
                and _text(touchpoint.get("touchpoint_id")) is not None
            ),
            "causal_target": causal_target,
            "runner_attested": True,
        },
        hash_field="executed_consumer_sha256",
    )


def _adapter_mechanism_evidence_receipt(
    proof: Mapping[str, Any],
    *,
    hypothesis_symbols: list[str],
    atom_bindings: Sequence[Mapping[str, Any]],
    clean_replays: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    graph = proof.get("mechanism_graph")
    nodes_raw = graph.get("nodes") if isinstance(graph, Mapping) else None
    edges_raw = graph.get("edges") if isinstance(graph, Mapping) else None
    root_node_id = graph.get("root_node_id") if isinstance(graph, Mapping) else None
    outcome_node_id = graph.get("outcome_node_id") if isinstance(graph, Mapping) else None
    mechanism_nodes = [
        dict(node)
        for node in (nodes_raw if isinstance(nodes_raw, list) else [])
        if isinstance(node, Mapping)
        and node.get("node_id") not in {root_node_id, outcome_node_id}
        and _text(node.get("locator")) is not None
    ]
    locators = [str(node["locator"]) for node in mechanism_nodes]
    if not locators or set(locators) != set(hypothesis_symbols):
        return None
    locator_by_node = {
        str(node["node_id"]): str(node["locator"])
        for node in mechanism_nodes
        if _text(node.get("node_id")) is not None
    }
    directed_edges = [
        {
            "from_locator": locator_by_node[str(edge["from_node_id"])],
            "to_locator": locator_by_node[str(edge["to_node_id"])],
            "kind": edge.get("kind"),
            "runner_attested": edge.get("runner_attested"),
            "evidence_sha256": edge.get("evidence_sha256"),
        }
        for edge in (edges_raw if isinstance(edges_raw, list) else [])
        if isinstance(edge, Mapping)
        and str(edge.get("from_node_id")) in locator_by_node
        and str(edge.get("to_node_id")) in locator_by_node
    ]
    link: dict[str, Any] = {
        "verification_method": "runner_causal_proof_adapter_v1",
        "adapter_id": proof.get("adapter_id"),
        "adapter_version": proof.get("adapter_version"),
        "proof_receipt_id": proof.get("proof_receipt_id"),
        "intervention_id": proof.get("intervention_id"),
        "entrypoint": locators[0],
        "code_path": [
            {
                "symbol": str(node["locator"]),
                "path": str(node["locator"]),
                "node_id": node.get("node_id"),
                "node_kind": node.get("kind"),
                "evidence_sha256": node.get("evidence_sha256"),
            }
            for node in mechanism_nodes
        ],
        "verified_directed_edges": directed_edges,
    }
    link["mechanism_link_sha256"] = _canonical_json_sha256(link)
    observations = proof.get("observations")
    experiment_ids = [
        str(observation.get("experiment_id"))
        for observation in (
            observations.get("baseline"),
            observations.get("challenge"),
        )
        if isinstance(observation, Mapping) and _text(observation.get("experiment_id")) is not None
    ] if isinstance(observations, Mapping) else []
    source_root = proof.get("source_root")
    origin_atom_ids = [
        atom_id
        for atom_id in (
            source_root.get("origin_atom_ids", [])
            if isinstance(source_root, Mapping)
            else []
        )
        if isinstance(atom_id, str)
    ]
    origin_symptom_bindings = [
        dict(binding)
        for binding in atom_bindings
        if isinstance(binding, Mapping)
        and binding.get("experiment_id") in experiment_ids
        and binding.get("atom_id") in origin_atom_ids
        and (
            (
                binding.get("binding_role") == "symptom"
                and not isinstance(binding.get("observation_predicate"), Mapping)
            )
            or (
                binding.get("binding_role") is None
                and _text(binding.get("match_kind")) is not None
            )
        )
    ]
    attested_predicate_bindings = (
        source_root.get("atom_field_predicate_bindings")
        if isinstance(source_root, Mapping)
        else None
    )
    if isinstance(attested_predicate_bindings, list):
        origin_symptom_bindings.extend(
            dict(binding)
            for binding in attested_predicate_bindings
            if isinstance(binding, Mapping)
            and binding.get("atom_id") in origin_atom_ids
        )
    if not experiment_ids or not origin_atom_ids or not origin_symptom_bindings:
        return None
    intervention = proof.get("intervention")
    target = _text(intervention.get("target")) if isinstance(intervention, Mapping) else None
    if target is None:
        return None
    adapter_evidence = proof.get("adapter_evidence")
    implementation_touchpoints = (
        adapter_evidence.get("implementation_touchpoints")
        if isinstance(adapter_evidence, Mapping)
        and isinstance(adapter_evidence.get("implementation_touchpoints"), list)
        else []
    )
    executed_consumer = _adapter_executed_consumer_receipt(
        proof,
        clean_replays=clean_replays,
        implementation_touchpoints=[
            touchpoint
            for touchpoint in implementation_touchpoints
            if isinstance(touchpoint, Mapping)
        ],
    )
    consumer_identity = (
        executed_consumer["consumer_identity"]
        if isinstance(executed_consumer, Mapping)
        else {"kind": "unresolved_consumer", "entrypoint": locators[0]}
    )
    receipt: dict[str, Any] = {
        "evidence_type": "adapter_proof",
        "hypothesis_id": proof.get("hypothesis_id"),
        "mechanism_symbols": locators,
        "mechanism_targets": mechanism_nodes,
        "code_paths": link["code_path"],
        "experiment_ids": experiment_ids,
        "artifact_refs": [
            artifact.get("artifact_id")
            for artifact in proof.get("artifacts", [])
            if isinstance(artifact, Mapping) and _text(artifact.get("artifact_id")) is not None
        ],
        "origin_atom_ids": sorted(set(origin_atom_ids)),
        "origin_symptom_bindings": origin_symptom_bindings,
        "path_name": locators[0],
        "consumer_identity": consumer_identity,
        "independence_key": _canonical_json_sha256(consumer_identity),
        "causal_target": target,
        "observed_result": proof.get("positive_outcome"),
        "mechanism_link": link,
        "causal_root_bindings": [
            {
                "kind": source_root.get("root_kind"),
                "origin_atom_ids": sorted(set(origin_atom_ids)),
                "source_root_sha256": source_root.get("source_root_sha256"),
                "root_mechanism_symbol": locators[0],
                "runner_attested": True,
            }
        ],
        "intervention_targets": [
            {
                "intervention_id": proof.get("intervention_id"),
                "kind": intervention.get("kind") if isinstance(intervention, Mapping) else None,
                "target": target,
            }
        ],
        "proof_receipt_id": proof.get("proof_receipt_id"),
        "adapter_id": proof.get("adapter_id"),
        "adversarial_effect": "supports_selection",
    }
    if isinstance(executed_consumer, Mapping):
        receipt["path_name"] = consumer_identity["entrypoint"]
        receipt["executed_consumer"] = dict(executed_consumer)
    if isinstance(implementation_touchpoints, list) and implementation_touchpoints:
        receipt["implementation_touchpoints"] = [
            dict(touchpoint)
            for touchpoint in implementation_touchpoints
            if isinstance(touchpoint, Mapping)
        ]
    receipt["mechanism_evidence_id"] = _content_addressed_receipt_id(
        "mechanism_evidence",
        receipt,
        "mechanism_evidence_id",
    )
    return receipt


def _typed_mechanism_evidence_receipts(
    dossier: dict[str, Any],
    *,
    clean_replays: dict[str, dict[str, Any]],
    symbol_receipts: list[dict[str, str]],
    causal_links: list[dict[str, Any]],
    strong_controls: list[dict[str, Any]],
    falsification_interventions: list[dict[str, Any]],
    deterministic_closures: list[dict[str, Any]],
    atom_bindings: list[dict[str, Any]],
    errors: list[str],
    proof_adapter_receipts: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Build practical, runner-bound evidence for researched mechanisms.

    Exact pytest/AST controls remain useful and are projected as
    ``controlled_scenario`` evidence, but they are no longer the universal proof
    shape.  Non-throwing output, retained harnesses, deterministic static traces,
    and correctly-platformed runtime observations can establish the same causal
    chain when their commands, artifacts, code reads, and alternatives are bound.
    """

    symbols_to_paths = {
        str(receipt.get("symbol")): str(receipt.get("path"))
        for receipt in symbol_receipts
        if _text(receipt.get("symbol")) is not None and _text(receipt.get("path")) is not None
    }
    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    links_by_experiment_symbol = {
        (
            str(link.get("hypothesis_id")),
            str(link.get("experiment_id")),
            str(link.get("symbol")),
        ): link
        for link in causal_links
        if isinstance(link, dict)
    }
    strong_by_pair = {
        (str(control.get("hypothesis_id")), str(control.get("control_experiment_id"))): control
        for control in strong_controls
        if isinstance(control, dict)
    }
    intervention_support: dict[tuple[str, str], set[str]] = {}
    for receipt in falsification_interventions:
        if not isinstance(receipt, dict):
            continue
        receipt_symbols = {
            symbol for symbol in receipt.get("mechanism_symbols", []) if isinstance(symbol, str)
        }
        for experiment_id in (
            receipt.get("baseline_experiment_id"),
            receipt.get("challenge_experiment_id"),
        ):
            if isinstance(experiment_id, str):
                intervention_support.setdefault(
                    (str(receipt.get("hypothesis_id")), experiment_id), set()
                ).update(receipt_symbols)
    deterministic_support: dict[tuple[str, str], set[str]] = {}
    for receipt in deterministic_closures:
        if not isinstance(receipt, dict):
            continue
        experiment_id = receipt.get("support_experiment_id")
        if not isinstance(experiment_id, str):
            continue
        deterministic_support.setdefault(
            (str(receipt.get("hypothesis_id")), experiment_id), set()
        ).update(
            symbol for symbol in receipt.get("mechanism_symbols", []) if isinstance(symbol, str)
        )
    symptom_bindings_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for binding in atom_bindings:
        if not isinstance(binding, dict):
            continue
        experiment_id = _text(binding.get("experiment_id"))
        match_kind = _text(binding.get("match_kind")) or ""
        is_symptom = binding.get("binding_role") == "symptom" or (
            "explicit_" not in match_kind and match_kind != ""
        )
        if experiment_id is not None and is_symptom:
            symptom_bindings_by_experiment.setdefault(experiment_id, []).append(dict(binding))
    receipts: list[dict[str, Any]] = []
    support_links: dict[tuple[str, str], dict[str, Any]] = {}
    support_consumers: dict[tuple[str, str], dict[str, str]] = {}
    support_verified_symbols: dict[tuple[str, str], set[str]] = {}

    def has_complementary_control(
        hypothesis: dict[str, Any],
        *,
        support_id: str,
        mechanism_symbols: list[str],
    ) -> list[str]:
        support = experiments.get(support_id)
        support_replay = clean_replays.get(support_id)
        if not isinstance(support, dict) or not isinstance(support_replay, dict):
            return []
        controlled: set[str] = set()
        counter_raw = hypothesis.get("counterevidence")
        for raw_counter_id in counter_raw if isinstance(counter_raw, list) else []:
            counter_id = _text(raw_counter_id)
            control = experiments.get(counter_id or "")
            control_replay = clean_replays.get(counter_id or "")
            relationship = (
                control.get("control_relationship")
                if isinstance(control, dict)
                and isinstance(control.get("control_relationship"), dict)
                else {}
            )
            if (
                counter_id is None
                or not isinstance(control, dict)
                or not isinstance(control_replay, dict)
                or control.get("scenario_kind") != "control"
                or control.get("outcome") != "refutes"
                or relationship.get("supports_experiment_id") != support_id
            ):
                continue
            relationship_symbols = _declared_mechanism_subset(
                relationship.get("mechanism_symbols"),
                hypothesis_symbols=mechanism_symbols,
            )
            if relationship_symbols is None:
                continue
            strong = strong_by_pair.get(
                (str(hypothesis.get("hypothesis_id") or "unknown"), counter_id)
            )
            if isinstance(strong, dict):
                controlled.update(
                    symbol
                    for symbol in strong.get("shared_verified_mechanism_symbols", [])
                    if symbol in relationship_symbols
                )
        pair = (str(hypothesis.get("hypothesis_id") or "unknown"), support_id)
        controlled.update(intervention_support.get(pair, set()))
        controlled.update(deterministic_support.get(pair, set()))
        return [symbol for symbol in mechanism_symbols if symbol in controlled]

    for hypothesis_index, hypothesis in enumerate(hypotheses):
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = _text(hypothesis.get("hypothesis_id")) or f"index-{hypothesis_index}"
        mechanism_raw = hypothesis.get("mechanism_symbols")
        mechanism_symbols = (
            [
                symbol.strip()
                for symbol in mechanism_raw
                if isinstance(symbol, str) and symbol.strip()
            ]
            if isinstance(mechanism_raw, list)
            else []
        )
        duplicate_symbols = sorted(
            {symbol for symbol in mechanism_symbols if mechanism_symbols.count(symbol) > 1}
        )
        if duplicate_symbols:
            errors.append(
                f"hypothesis_mechanism_symbols_duplicate:{hypothesis_id}:"
                + ",".join(duplicate_symbols)
            )
            continue
        support_raw = hypothesis.get("supporting_evidence")
        support_ids: list[Any] = []
        for support_ref in support_raw if isinstance(support_raw, list) else []:
            if support_ref not in support_ids:
                support_ids.append(support_ref)
        hypothesis_receipt_count = 0
        aggregate_verified_symbols: set[str] = set()
        hypothesis_support_receipts: list[dict[str, Any]] = []

        for proof in proof_adapter_receipts:
            if not isinstance(proof, Mapping) or proof.get("hypothesis_id") != hypothesis_id:
                continue
            adapter_receipt = _adapter_mechanism_evidence_receipt(
                proof,
                hypothesis_symbols=mechanism_symbols,
                atom_bindings=atom_bindings,
                clean_replays=clean_replays,
            )
            if adapter_receipt is None or not set(
                adapter_receipt.get("experiment_ids", [])
            ).intersection(str(value) for value in support_ids):
                continue
            hypothesis_support_receipts.append(adapter_receipt)

        adapter_evidence_present = bool(hypothesis_support_receipts)
        for raw_support_id in ([] if adapter_evidence_present else support_ids):
            support_id = _text(raw_support_id)
            experiment = experiments.get(support_id or "")
            replay = clean_replays.get(support_id or "")
            if (
                support_id is None
                or not isinstance(experiment, dict)
                or not isinstance(replay, dict)
                or experiment.get("outcome") != "supports"
                or replay.get("assertion_passed") is not True
            ):
                continue
            if isinstance(experiment.get("proof_adapter"), Mapping):
                continue
            scenario_kind = str(experiment.get("scenario_kind") or "")
            assertion_raw = experiment.get("observable_assertion")
            observable_assertion = assertion_raw if isinstance(assertion_raw, dict) else {}
            harness_path, touched_symbols, harness_link = _harness_mechanism_touches(
                replay=replay,
                mechanism_symbols=mechanism_symbols,
                symbol_paths=symbols_to_paths,
                observable_assertion=observable_assertion,
            )
            harness_symbols = [symbol for symbol in mechanism_symbols if symbol in touched_symbols]
            declared_raw = experiment.get("mechanism_link")
            declared_steps = (
                declared_raw.get("code_path")
                if isinstance(declared_raw, dict)
                and isinstance(declared_raw.get("code_path"), list)
                else []
            )
            declared_step_symbols = {
                str(step.get("symbol")) for step in declared_steps if isinstance(step, dict)
            }
            declared_symbols = [
                symbol for symbol in mechanism_symbols if symbol in declared_step_symbols
            ]
            declared_link = (
                _verified_declared_mechanism_link(
                    experiment=experiment,
                    mechanism_symbols=declared_symbols,
                    symbol_paths=symbols_to_paths,
                    workspace=(
                        Path(str(replay["workspace_dir"]))
                        if _text(replay.get("workspace_dir")) is not None
                        else None
                    ),
                )
                if declared_symbols
                else None
            )
            if declared_link is None:
                declared_symbols = []
            declared_entrypoint = (
                _text(declared_link.get("entrypoint")) if isinstance(declared_link, dict) else None
            )
            declared_link_reached = isinstance(declared_link, dict) and (
                harness_path is None or declared_entrypoint in set(harness_symbols)
            )
            trace_symbols = [
                symbol
                for symbol in mechanism_symbols
                if (hypothesis_id, support_id, symbol) in links_by_experiment_symbol
            ]
            trace_link = None
            if trace_symbols:
                trace_link = {
                    "verification_method": "runner_exception_symbol_trace_v1",
                    "entrypoint": trace_symbols[0],
                    "code_path": [
                        {
                            "symbol": symbol,
                            "path": symbols_to_paths[symbol],
                            "trace_excerpt_sha256": links_by_experiment_symbol[
                                (hypothesis_id, support_id, symbol)
                            ].get("trace_excerpt_sha256"),
                        }
                        for symbol in trace_symbols
                    ],
                }
            required_platform = _text(experiment.get("platform_requirement")) or "any"
            isolation_raw = replay.get("execution_isolation")
            isolation = isolation_raw if isinstance(isolation_raw, dict) else {}
            actual_platform = _text(isolation.get("platform")) or "unknown"
            verified_symbols: list[str] = []
            mechanism_link: dict[str, Any] | None = None

            if scenario_kind == "live_runtime":
                if (
                    required_platform == "any"
                    or required_platform.casefold() != actual_platform.casefold()
                ):
                    errors.append(
                        f"live_runtime_platform_unverified:{hypothesis_id}:{support_id}:"
                        f"required={required_platform}:actual={actual_platform}"
                    )
                    continue
                candidates = [
                    (harness_symbols, harness_link),
                    (trace_symbols, trace_link),
                ]
                candidates = [
                    (symbols, link)
                    for symbols, link in candidates
                    if symbols and isinstance(link, dict)
                ]
                if not candidates:
                    if not declared_link_reached:
                        errors.append(
                            f"live_runtime_mechanism_link_missing:{hypothesis_id}:{support_id}"
                        )
                        continue
                if declared_link_reached:
                    verified_symbols, mechanism_link = declared_symbols, declared_link
                else:
                    verified_symbols, mechanism_link = max(
                        candidates,
                        key=lambda item: len(item[0]),
                    )
                evidence_type = "live_runtime"
            elif scenario_kind == "static_trace":
                static_raw = experiment.get("static_trace")
                static_trace = static_raw if isinstance(static_raw, dict) else {}
                dependencies = static_trace.get("environment_dependencies")
                trace_steps = static_trace.get("code_path")
                traced_pairs = {
                    (str(step.get("symbol")), str(step.get("path")))
                    for step in (trace_steps if isinstance(trace_steps, list) else [])
                    if isinstance(step, dict)
                }
                expected_pairs = {
                    (symbol, symbols_to_paths.get(symbol)) for symbol in mechanism_symbols
                }
                static_symbols = [
                    symbol
                    for symbol in mechanism_symbols
                    if (symbol, symbols_to_paths.get(symbol)) in traced_pairs
                ]
                callable_symbols = [
                    symbol for symbol in static_symbols if not symbol.startswith("config:")
                ]
                if (
                    static_trace.get("deterministic") is not True
                    or not isinstance(dependencies, list)
                    or dependencies
                    or not isinstance(trace_steps, list)
                    or not trace_steps
                    or not traced_pairs.issubset(expected_pairs)
                    or not static_symbols
                    or (
                        callable_symbols
                        and (
                            (
                                harness_path is None
                                or not set(callable_symbols).issubset(set(touched_symbols))
                            )
                            and not set(callable_symbols).issubset(set(declared_symbols))
                        )
                    )
                ):
                    errors.append(
                        f"static_trace_not_deterministic_or_unbound:{hypothesis_id}:{support_id}"
                    )
                    continue
                verified_symbols = static_symbols
                mechanism_link = {
                    "verification_method": "runner_deterministic_static_trace_v1",
                    "entrypoint": static_symbols[0],
                    "code_path": trace_steps,
                    "environment_dependencies": dependencies,
                }
                mechanism_link["static_trace_sha256"] = _canonical_json_sha256(mechanism_link)
                evidence_type = "static_trace"
            elif harness_path is not None:
                if not harness_symbols or harness_link is None:
                    errors.append(
                        f"temporary_harness_mechanism_call_missing:{hypothesis_id}:{support_id}"
                    )
                    continue
                if declared_link_reached:
                    verified_symbols = declared_symbols
                    mechanism_link = declared_link
                else:
                    verified_symbols = harness_symbols
                    mechanism_link = harness_link
                evidence_type = "temporary_harness"
            elif trace_symbols and trace_link is not None:
                verified_symbols = trace_symbols
                mechanism_link = trace_link
                evidence_type = "exception_trace"
            else:
                # A wrong value, omitted artifact, or bad classification normally
                # fails at the assertion boundary, not inside the mechanism.  The
                # verified replay plus exact inspected code path is the relevant
                # evidence; absence of a production traceback is not a defect.
                pair = (hypothesis_id, support_id)
                intervention_symbols = [
                    symbol
                    for symbol in mechanism_symbols
                    if symbol in intervention_support.get(pair, set())
                ]
                if declared_link is None and intervention_symbols:
                    verified_symbols = intervention_symbols
                    intervention_ids = sorted(
                        str(item.get("intervention_receipt_id"))
                        for item in falsification_interventions
                        if isinstance(item, dict)
                        and item.get("hypothesis_id") == hypothesis_id
                        and support_id
                        in {
                            item.get("baseline_experiment_id"),
                            item.get("challenge_experiment_id"),
                        }
                        and set(intervention_symbols).issubset(
                            set(item.get("shared_verified_mechanism_symbols", []))
                        )
                    )
                    mechanism_link = {
                        "verification_method": "runner_falsification_shared_mechanism_v1",
                        "entrypoint": intervention_symbols[0],
                        "code_path": [
                            {"symbol": symbol, "path": symbols_to_paths[symbol]}
                            for symbol in intervention_symbols
                            if symbol in symbols_to_paths
                        ],
                        "intervention_receipt_ids": intervention_ids,
                    }
                    evidence_type = "observed_output"
                elif declared_link is None:
                    errors.append(
                        f"observed_output_mechanism_link_missing:{hypothesis_id}:{support_id}"
                    )
                    continue
                else:
                    controlled_symbols = has_complementary_control(
                        hypothesis,
                        support_id=support_id,
                        mechanism_symbols=declared_symbols,
                    )
                    if not controlled_symbols:
                        errors.append(
                            f"observed_output_complementary_control_missing:"
                            f"{hypothesis_id}:{support_id}"
                        )
                        continue
                    verified_symbols = controlled_symbols
                    mechanism_link = declared_link
                    evidence_type = "observed_output"

            code_paths = [
                {"symbol": symbol, "path": symbols_to_paths[symbol]}
                for symbol in verified_symbols
                if symbol in symbols_to_paths
            ]
            consumer_identity = _experiment_consumer_identity(
                experiment=experiment,
                replay=replay,
                mechanism_link=mechanism_link,
                harness_path=harness_path,
            )
            support_links[(hypothesis_id, support_id)] = mechanism_link or {}
            support_consumers[(hypothesis_id, support_id)] = consumer_identity
            support_verified_symbols[(hypothesis_id, support_id)] = set(verified_symbols)
            origin_symptom_bindings = sorted(
                symptom_bindings_by_experiment.get(support_id, []),
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )

            receipt: dict[str, Any] = {
                "evidence_type": evidence_type,
                "hypothesis_id": hypothesis_id,
                "mechanism_symbols": verified_symbols,
                "code_paths": code_paths,
                "experiment_ids": [support_id],
                "artifact_refs": experiment.get("artifact_refs", []),
                "origin_atom_ids": sorted(set(experiment.get("addresses_atom_ids", []))),
                "origin_symptom_bindings": origin_symptom_bindings,
                "executed_argv": replay.get("executed_argv"),
                "command_authorization": replay.get("command_authorization"),
                "path_name": consumer_identity["entrypoint"],
                "consumer_identity": consumer_identity,
                "independence_key": _canonical_json_sha256(consumer_identity),
                "observed_result": {
                    "exit_code": replay.get("exit_code"),
                    "stdout_sha256": replay.get("stdout_sha256"),
                    "stderr_sha256": replay.get("stderr_sha256"),
                    "assertion": experiment.get("observable_assertion"),
                },
                "harness_path": harness_path,
                "mechanism_link": mechanism_link,
                "platform_requirement": required_platform,
                "observed_platform": actual_platform,
                "adversarial_effect": "supports_selection",
            }
            receipt["causal_root_bindings"] = _derived_causal_root_bindings(
                experiment_ids=[support_id],
                origin_atom_ids=list(receipt["origin_atom_ids"]),
                origin_symptom_bindings=origin_symptom_bindings,
                mechanism_symbols=verified_symbols,
                mechanism_link=mechanism_link,
                executed_argv=(
                    list(replay["executed_argv"])
                    if isinstance(replay.get("executed_argv"), list)
                    else None
                ),
                command_authorization=(
                    dict(replay["command_authorization"])
                    if isinstance(replay.get("command_authorization"), dict)
                    else None
                ),
            )
            receipt["mechanism_evidence_id"] = _content_addressed_receipt_id(
                "mechanism_evidence",
                receipt,
                "mechanism_evidence_id",
            )
            hypothesis_support_receipts.append(receipt)

        (
            connected_supports,
            aggregate_verified_symbols,
            _support_connectivity,
            disconnected_support_ids,
        ) = _rooted_support_connectivity(
            hypothesis_support_receipts,
            hypothesis_symbols=mechanism_symbols,
        )
        receipts.extend(connected_supports)
        hypothesis_receipt_count = len(connected_supports)
        support_by_id = {
            str(item.get("mechanism_evidence_id")): item
            for item in hypothesis_support_receipts
            if _text(item.get("mechanism_evidence_id")) is not None
        }
        if hypothesis_index == 0:
            for evidence_id in disconnected_support_ids:
                disconnected = support_by_id.get(evidence_id, {})
                experiment_ids = [
                    value
                    for value in disconnected.get("experiment_ids", [])
                    if isinstance(value, str)
                ]
                label = experiment_ids[0] if experiment_ids else evidence_id
                errors.append(f"primary_hypothesis_support_disconnected:{hypothesis_id}:{label}")

        counter_raw = hypothesis.get("counterevidence")
        counter_ids = (
            []
            if adapter_evidence_present
            else counter_raw if isinstance(counter_raw, list) else []
        )
        for raw_control_id in counter_ids:
            control_id = _text(raw_control_id)
            control = experiments.get(control_id or "")
            if (
                control_id is None
                or not isinstance(control, dict)
                or control.get("scenario_kind") != "control"
                or control.get("outcome") != "refutes"
            ):
                continue
            relationship_raw = control.get("control_relationship")
            relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
            relationship_symbols = _declared_mechanism_subset(
                relationship.get("mechanism_symbols"),
                hypothesis_symbols=mechanism_symbols,
            )
            support_id = _text(relationship.get("supports_experiment_id"))
            support = experiments.get(support_id or "")
            support_replay = clean_replays.get(support_id or "")
            control_replay = clean_replays.get(control_id)
            if (
                support_id is None
                or not isinstance(support, dict)
                or not isinstance(support_replay, dict)
                or not isinstance(control_replay, dict)
                or relationship_symbols is None
            ):
                continue
            observable_errors: list[str] = []
            observable = _observable_controlled_difference(
                hypothesis_id=hypothesis_id,
                control_id=control_id,
                support=support,
                control=control,
                support_replay=support_replay,
                control_replay=control_replay,
                errors=observable_errors,
            )
            if observable is None:
                continue
            strong = strong_by_pair.get((hypothesis_id, control_id))
            intervention = next(
                (
                    item
                    for item in falsification_interventions
                    if isinstance(item, dict)
                    and item.get("hypothesis_id") == hypothesis_id
                    and item.get("baseline_experiment_id") == support_id
                    and item.get("challenge_experiment_id") == control_id
                    and item.get("shared_verified_mechanism_symbols") == relationship_symbols
                ),
                None,
            )
            if isinstance(strong, dict):
                independently_verified = (
                    strong.get("shared_verified_mechanism_symbols") == relationship_symbols
                )
            else:
                independently_verified = isinstance(intervention, dict)
            support_link = support_links.get((hypothesis_id, support_id))
            support_symbols = support_verified_symbols.get((hypothesis_id, support_id), set())
            control_link: dict[str, Any] | None = None
            if not independently_verified and set(relationship_symbols).issubset(support_symbols):
                control_link = _verified_declared_mechanism_link(
                    experiment=control,
                    mechanism_symbols=relationship_symbols,
                    symbol_paths=symbols_to_paths,
                    workspace=(
                        Path(str(control_replay["workspace_dir"]))
                        if _text(control_replay.get("workspace_dir")) is not None
                        else None
                    ),
                )
                support_method = (
                    _text(support_link.get("verification_method"))
                    if isinstance(support_link, dict)
                    else None
                )
                control_method = (
                    _text(control_link.get("verification_method"))
                    if isinstance(control_link, dict)
                    else None
                )
                if support_method is not None and support_method == control_method:
                    independently_verified = True
                elif support_method is not None and control_method is not None:
                    errors.append(
                        f"controlled_scenario_mechanism_mode_switch:{hypothesis_id}:{control_id}"
                    )
            support_consumer = support_consumers.get((hypothesis_id, support_id))
            if not independently_verified or (strong is None and not support_link):
                errors.append(
                    f"controlled_scenario_mechanism_link_missing:{hypothesis_id}:{control_id}"
                )
                continue
            if support_consumer is None:
                support_consumer = _experiment_consumer_identity(
                    experiment=support,
                    replay=support_replay,
                    mechanism_link=support_link,
                    harness_path=None,
                )
            relationship_code_paths = [
                {"symbol": symbol, "path": symbols_to_paths[symbol]}
                for symbol in relationship_symbols
                if symbol in symbols_to_paths
            ]
            receipt = {
                "evidence_type": "controlled_scenario",
                "hypothesis_id": hypothesis_id,
                "mechanism_symbols": relationship_symbols,
                "code_paths": relationship_code_paths,
                "experiment_ids": [support_id, control_id],
                "artifact_refs": sorted(
                    set(support.get("artifact_refs", [])) | set(control.get("artifact_refs", []))
                ),
                "origin_atom_ids": sorted(set(support.get("addresses_atom_ids", []))),
                "path_name": support_consumer["entrypoint"],
                "consumer_identity": support_consumer,
                "independence_key": _canonical_json_sha256(support_consumer),
                "controlled_condition": {
                    "variable": relationship.get("controlled_variable"),
                    "expected_difference": relationship.get("expected_difference"),
                },
                "observable_difference": observable,
                "strong_pytest_control_id": (
                    strong.get("control_verification_id") if isinstance(strong, dict) else None
                ),
                "falsification_intervention_id": (
                    intervention.get("intervention_receipt_id")
                    if isinstance(intervention, dict)
                    else None
                ),
                "mechanism_link": support_link,
                "control_mechanism_link": control_link,
                "adversarial_effect": "limits_scope",
            }
            receipt["mechanism_evidence_id"] = _content_addressed_receipt_id(
                "mechanism_evidence",
                receipt,
                "mechanism_evidence_id",
            )
            receipts.append(receipt)

        # The first hypothesis is the implementation-driving hypothesis.  Later
        # hypotheses are explicit alternatives and may be rejected by retained
        # artifacts/counterevidence rather than their own reproduction.
        if hypothesis_index == 0 and hypothesis_receipt_count == 0:
            errors.append(f"primary_hypothesis_mechanism_evidence_missing:{hypothesis_id}")
        if hypothesis_index == 0:
            missing_symbols = [
                symbol for symbol in mechanism_symbols if symbol not in aggregate_verified_symbols
            ]
            if missing_symbols:
                errors.append(
                    f"primary_hypothesis_mechanism_coverage_incomplete:{hypothesis_id}:"
                    + ",".join(missing_symbols)
                )
            if not any(
                item.get("connection_kind") == "causal_root" for item in _support_connectivity
            ):
                errors.append(f"primary_hypothesis_causal_root_missing:{hypothesis_id}")

    receipts.sort(
        key=lambda receipt: (
            str(receipt.get("hypothesis_id")),
            str(receipt.get("evidence_type")),
            str(receipt.get("mechanism_evidence_id")),
        )
    )
    return receipts


def _verified_mechanism_projection(
    dossier: dict[str, Any],
    *,
    mechanism_evidence: list[dict[str, Any]],
    control_verifications: list[dict[str, Any]],
    falsification_interventions: list[dict[str, Any]],
    deterministic_closures: list[dict[str, Any]],
) -> tuple[
    dict[str, Any] | None,
    str | None,
    dict[str, Any] | None,
    str | None,
]:
    """Derive cross-case mechanism identity from runner-minted causal facts.

    Hypothesis IDs and content-addressed receipt IDs are case/run provenance.  If
    they participate in this digest, two independent dossiers that establish the
    same code mechanism can never become a same-cause relation.  Keep that
    provenance in the surrounding evidence receipt and project only normalized
    symbols and exact repository paths.  Research probe slots are useful proof
    provenance, but are not necessarily production branch identity; hashing them
    would split the same mechanism when two researchers choose different probes.
    """

    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    primary = hypotheses[0] if hypotheses and isinstance(hypotheses[0], dict) else None
    if not isinstance(primary, dict):
        return None, None, None, None
    hypothesis_id = _text(primary.get("hypothesis_id"))
    symbols_raw = primary.get("mechanism_symbols")
    symbols = (
        [symbol.strip() for symbol in symbols_raw if isinstance(symbol, str) and symbol.strip()]
        if isinstance(symbols_raw, list)
        else []
    )
    if len(symbols) != len(set(symbols)):
        return None, None, None, None
    candidate_evidence: list[dict[str, Any]] = []
    for item in mechanism_evidence:
        if (
            not isinstance(item, dict)
            or item.get("hypothesis_id") != hypothesis_id
            or item.get("adversarial_effect") != "supports_selection"
            or _text(item.get("mechanism_evidence_id")) is None
        ):
            continue
        item_symbols = _declared_mechanism_subset(
            item.get("mechanism_symbols"),
            hypothesis_symbols=symbols,
        )
        if item_symbols is None:
            continue
        points_raw = item.get("code_paths")
        points = points_raw if isinstance(points_raw, list) else []
        point_symbols = {
            str(point.get("symbol"))
            for point in points
            if isinstance(point, dict)
            and _text(point.get("symbol")) is not None
            and _text(point.get("path")) is not None
        }
        if point_symbols != set(item_symbols):
            continue
        symptom_raw = item.get("origin_symptom_bindings")
        origin_raw = item.get("origin_atom_ids")
        experiments_raw = item.get("experiment_ids")
        argv_raw = item.get("executed_argv")
        authorization_raw = item.get("command_authorization")
        declared_roots = item.get("causal_root_bindings")
        derived_roots = (
            [dict(value) for value in declared_roots if isinstance(value, dict)]
            if item.get("evidence_type") == "adapter_proof"
            and isinstance(declared_roots, list)
            else _derived_causal_root_bindings(
                experiment_ids=(
                    [value for value in experiments_raw if isinstance(value, str)]
                    if isinstance(experiments_raw, list)
                    else []
                ),
                origin_atom_ids=(
                    [value for value in origin_raw if isinstance(value, str)]
                    if isinstance(origin_raw, list)
                    else []
                ),
                origin_symptom_bindings=(
                    [dict(value) for value in symptom_raw if isinstance(value, dict)]
                    if isinstance(symptom_raw, list)
                    else []
                ),
                mechanism_symbols=item_symbols,
                mechanism_link=(
                    dict(item["mechanism_link"])
                    if isinstance(item.get("mechanism_link"), dict)
                    else None
                ),
                executed_argv=(
                    [value for value in argv_raw if isinstance(value, str)]
                    if isinstance(argv_raw, list)
                    else None
                ),
                command_authorization=(
                    dict(authorization_raw) if isinstance(authorization_raw, dict) else None
                ),
            )
        )
        if declared_roots is not None and declared_roots != derived_roots:
            continue
        projected_item = dict(item)
        projected_item["causal_root_bindings"] = derived_roots
        candidate_evidence.append(projected_item)
    (
        evidence,
        covered_symbols,
        support_connectivity,
        _disconnected_support_ids,
    ) = _rooted_support_connectivity(
        candidate_evidence,
        hypothesis_symbols=symbols,
    )
    paths = sorted(
        {
            (
                str(point.get("symbol")).strip(),
                PurePosixPath(
                    str(point.get("path")).replace("\\", "/").removeprefix("./")
                ).as_posix(),
            )
            for item in evidence
            for point in (
                item.get("code_paths") if isinstance(item.get("code_paths"), list) else []
            )
            if isinstance(point, dict)
            and _text(point.get("symbol")) is not None
            and _text(point.get("path")) is not None
        }
    )
    normalized_symbols = sorted({symbol.strip() for symbol in symbols if symbol.strip()})
    if (
        hypothesis_id is None
        or not normalized_symbols
        or covered_symbols != set(normalized_symbols)
        or {symbol for symbol, _ in paths} != set(normalized_symbols)
        or not any(item.get("connection_kind") == "causal_root" for item in support_connectivity)
    ):
        return None, None, None, None
    control_points: list[dict[str, Any]] = []
    for causal_receipt in (*control_verifications, *falsification_interventions):
        if (
            not isinstance(causal_receipt, dict)
            or causal_receipt.get("hypothesis_id") != hypothesis_id
        ):
            continue
        receipt_symbols_raw = causal_receipt.get("mechanism_symbols")
        receipt_symbols = _declared_mechanism_subset(
            receipt_symbols_raw,
            hypothesis_symbols=symbols,
        )
        if receipt_symbols is None:
            continue
        controlled = causal_receipt.get("controlled_input_difference")
        difference = controlled.get("difference") if isinstance(controlled, dict) else None
        method = (
            _text(controlled.get("verification_method")) if isinstance(controlled, dict) else None
        )
        slot = _text(difference.get("slot")) if isinstance(difference, dict) else None
        if method is None or slot is None:
            continue
        descriptor: dict[str, Any] = {
            "verification_method": method,
            "mechanism_symbols": receipt_symbols,
            "slot": slot,
        }
        mechanism_symbol = (
            _text(difference.get("mechanism_symbol")) if isinstance(difference, dict) else None
        )
        if mechanism_symbol is not None:
            descriptor["mechanism_symbol"] = mechanism_symbol
        control_points.append(descriptor)
    unique_control_points = sorted(
        {
            json.dumps(value, sort_keys=True, separators=(",", ":")): value
            for value in control_points
        }.values(),
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    mechanism_targets = sorted(
        {
            json.dumps(target, sort_keys=True, separators=(",", ":")): target
            for item in evidence
            for target in (
                item.get("mechanism_targets")
                if isinstance(item.get("mechanism_targets"), list)
                else []
            )
            if isinstance(target, dict)
        }.values(),
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    projection = {
        "schema_version": 3,
        "mechanism_symbols": normalized_symbols,
        "code_paths": [{"symbol": symbol, "path": path} for symbol, path in paths],
    }
    if mechanism_targets:
        projection["mechanism_targets"] = mechanism_targets
    provenance = {
        "schema_version": 2,
        "primary_hypothesis_id": hypothesis_id,
        "mechanism_evidence_ids": sorted(str(item["mechanism_evidence_id"]) for item in evidence),
        "causal_root_evidence_ids": sorted(
            str(item["mechanism_evidence_id"])
            for item in evidence
            if item.get("causal_root_bindings")
        ),
        "support_connectivity": support_connectivity,
        "support_symbol_coverage": sorted(
            (
                {
                    "experiment_ids": sorted(
                        experiment_id
                        for experiment_id in item.get("experiment_ids", [])
                        if isinstance(experiment_id, str)
                    ),
                    "mechanism_symbols": item.get("mechanism_symbols"),
                }
                for item in evidence
            ),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
        "causal_control_ids": sorted(
            str(item["control_verification_id"])
            for item in control_verifications
            if isinstance(item, dict)
            and item.get("hypothesis_id") == hypothesis_id
            and _text(item.get("control_verification_id")) is not None
        ),
        "falsification_intervention_ids": sorted(
            str(item["intervention_receipt_id"])
            for item in falsification_interventions
            if isinstance(item, dict)
            and item.get("hypothesis_id") == hypothesis_id
            and _text(item.get("intervention_receipt_id")) is not None
        ),
        "deterministic_closure_ids": sorted(
            str(item["closure_receipt_id"])
            for item in deterministic_closures
            if isinstance(item, dict)
            and item.get("hypothesis_id") == hypothesis_id
            and _text(item.get("closure_receipt_id")) is not None
        ),
        "research_probe_control_points": unique_control_points,
    }
    intervention_targets = sorted(
        {
            json.dumps(target, sort_keys=True, separators=(",", ":")): target
            for item in evidence
            for target in (
                item.get("intervention_targets")
                if isinstance(item.get("intervention_targets"), list)
                else []
            )
            if isinstance(target, dict)
        }.values(),
        key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
    )
    if intervention_targets:
        provenance["intervention_targets"] = intervention_targets
    return (
        projection,
        _canonical_json_sha256(projection),
        provenance,
        _canonical_json_sha256(provenance),
    )


def _runs_root_for(path: Path) -> Path | None:
    resolved = path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.casefold() == "runs":
            return candidate
    return None


def _persist_outcome_overlay_asset(
    *,
    run_dir: Path,
    research_workspace: Path,
    overlay_manifest: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    """Retain the exact stage-3 overlay under a trusted, content-addressed run path."""

    runs_root = _runs_root_for(run_dir)
    if runs_root is None:
        # Nonstandard library callers can still produce valid research.  They simply
        # cannot export a retained-harness outcome oracle until the run is retained
        # below the normal runs root.
        return None
    normalized_manifest = {
        path: dict(entry)
        for path, entry in sorted(overlay_manifest.items())
        if isinstance(path, str) and isinstance(entry, dict)
    }
    if not normalized_manifest:
        return None
    asset_projection = {"schema_version": 1, "manifest": normalized_manifest}
    asset_id = f"outcome_asset:{_canonical_json_sha256(asset_projection)}"
    asset_dir = run_dir / "outcome_oracle_assets" / asset_id.split(":", 1)[1]
    bundle = asset_dir / "bundle"
    for relative, expected in normalized_manifest.items():
        source = (research_workspace / relative).resolve()
        destination = (bundle / relative).resolve()
        if (
            not relative.startswith(".usertest_research/")
            or expected.get("kind") != "file"
            or not _within(source, research_workspace.resolve())
            or not _within(destination, bundle.resolve())
            or not source.is_file()
            or source.is_symlink()
            or expected.get("sha256") != _sha256_path(source)
            or expected.get("size_bytes") != source.stat().st_size
        ):
            errors.append(f"outcome_oracle_asset_source_invalid:{relative}")
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if (
                not destination.is_file()
                or destination.is_symlink()
                or _sha256_path(destination) != expected.get("sha256")
                or destination.stat().st_size != expected.get("size_bytes")
            ):
                errors.append(f"outcome_oracle_asset_collision:{relative}")
                return None
        else:
            shutil.copy2(source, destination)
    observed = _workspace_manifest(bundle)
    if observed != normalized_manifest:
        errors.append("outcome_oracle_asset_manifest_mismatch")
        return None
    return {
        "asset_id": asset_id,
        "runs_relative_path": bundle.resolve().relative_to(runs_root).as_posix(),
        "manifest": normalized_manifest,
        "manifest_sha256": _canonical_json_sha256(normalized_manifest),
    }


def _replay_output(replay: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    for field in ("stdout_path", "stderr_path"):
        raw = _text(replay.get(field))
        path = Path(raw) if raw is not None else None
        if path is None or not path.is_file():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks).replace("\\", "/")


def _output_references_assertion(*, output: str, test_path: str, line: int) -> bool:
    normalized_path = test_path.replace("\\", "/").removeprefix("./")
    direct = re.compile(
        rf"(?:^|[\s\"']){re.escape(normalized_path)}:{line}(?::|\b)",
        re.MULTILINE,
    )
    native = re.compile(rf"File\s+[\"'][^\"']*{re.escape(normalized_path)}[\"'],\s*line\s+{line}\b")
    return direct.search(output) is not None or native.search(output) is not None


def _source_case_bindings(
    *,
    experiment: Mapping[str, Any],
    evidence_assignment: Mapping[str, Any],
    atom_bindings: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Retain the runner-verified fidelity mapping from a diagnostic to source evidence."""

    experiment_id = experiment.get("experiment_id")
    addressed = {
        atom_id for atom_id in experiment.get("addresses_atom_ids", []) if isinstance(atom_id, str)
    }
    receipts = {
        str(receipt.get("atom_id")): receipt
        for receipt in evidence_assignment.get("atom_receipts", [])
        if isinstance(receipt, dict)
    }
    result: list[dict[str, Any]] = []
    for binding in atom_bindings:
        atom_id = _text(binding.get("atom_id"))
        atom_receipt = receipts.get(atom_id or "")
        snapshot = atom_receipt.get("atom_snapshot") if isinstance(atom_receipt, dict) else None
        if (
            binding.get("experiment_id") != experiment_id
            or atom_id not in addressed
            or not _source_observation_atom(snapshot)
            or _text(binding.get("match_kind")) is None
        ):
            continue
        result.append(
            {
                key: binding.get(key)
                for key in (
                    "experiment_id",
                    "atom_id",
                    "match_kind",
                    "binding_role",
                    "origin_atom_sha256",
                    "origin_atom_field_path",
                    "origin_atom_value_sha256",
                    "origin_artifact_path",
                    "origin_artifact_sha256",
                )
                if binding.get(key) is not None
            }
        )
    return sorted(result, key=lambda item: (str(item.get("atom_id")), str(item)))


def _repository_test_positive_contract(
    *,
    experiment_id: str,
    experiment: dict[str, Any],
    replay: dict[str, Any],
    evidence: list[dict[str, Any]],
    evidence_assignment: Mapping[str, Any],
    atom_bindings: Sequence[Mapping[str, Any]],
    planning_workspace: Path | None,
) -> dict[str, Any] | None:
    """Mint a positive contract only from the original fail-first repository test."""

    baseline_exit = replay.get("exit_code")
    source_bindings = _source_case_bindings(
        experiment=experiment,
        evidence_assignment=evidence_assignment,
        atom_bindings=atom_bindings,
    )
    if (
        planning_workspace is None
        or not evidence
        or isinstance(baseline_exit, bool)
        or not isinstance(baseline_exit, int)
        or baseline_exit == 0
        or not source_bindings
    ):
        return None
    primary = evidence[0]
    hypothesis_id = _text(primary.get("hypothesis_id"))
    symbols_raw = primary.get("mechanism_symbols")
    mechanism_symbols = (
        [symbol for symbol in symbols_raw if isinstance(symbol, str) and symbol.strip()]
        if isinstance(symbols_raw, list)
        else []
    )
    paths = {
        str(item.get("symbol")): str(item.get("path"))
        for item in primary.get("code_paths", [])
        if isinstance(item, dict)
        and _text(item.get("symbol")) is not None
        and _text(item.get("path")) is not None
    }
    entrypoint = mechanism_symbols[0] if mechanism_symbols else None
    if hypothesis_id is None or entrypoint is None or entrypoint not in paths:
        return None
    optional_errors: list[str] = []
    selection = _pytest_test_selection_receipt(
        hypothesis_id=hypothesis_id,
        experiment_id=experiment_id,
        experiment=experiment,
        replay=replay,
        mechanism_symbols=[entrypoint],
        symbol_paths={entrypoint: paths[entrypoint]},
        planning_workspace=planning_workspace,
        errors=optional_errors,
    )
    if not isinstance(selection, dict):
        return None
    output = _replay_output(replay)
    assertions_raw = selection.get("semantic_assertions")
    assertions = [
        assertion
        for assertion in (assertions_raw if isinstance(assertions_raw, list) else [])
        if isinstance(assertion, dict)
        and entrypoint
        in (
            assertion.get("mechanism_symbols")
            if isinstance(assertion.get("mechanism_symbols"), list)
            else []
        )
        and isinstance(assertion.get("line"), int)
        and _output_references_assertion(
            output=output,
            test_path=str(selection.get("test_path") or ""),
            line=int(assertion["line"]),
        )
    ]
    if not assertions:
        return None
    contract: dict[str, Any] = {
        "schema_version": 1,
        "kind": "repository_test_assertion",
        "research_experiment_id": experiment_id,
        "mechanism_evidence_ids": sorted(
            {
                str(item["mechanism_evidence_id"])
                for item in evidence
                if _text(item.get("mechanism_evidence_id")) is not None
            }
        ),
        "source_case_bindings": source_bindings,
        "repository_contract": {
            "runner": "pytest",
            "test_path": selection.get("test_path"),
            "test_file_sha256": selection.get("test_file_sha256"),
            "test_file_git_blob_sha": selection.get("test_file_git_blob_sha"),
            "selector": selection.get("selector"),
            "test_function": selection.get("test_function"),
            "test_function_source_sha256": selection.get("test_function_source_sha256"),
            "reachable_function_contracts": selection.get("reachable_function_contracts"),
            "relevant_module_imports_sha256": selection.get("relevant_module_imports_sha256"),
            "mechanism_touches": selection.get("mechanism_touches"),
            "semantic_assertions": assertions,
        },
        "baseline_failure": {
            "exit_code": baseline_exit,
            "stdout_sha256": replay.get("stdout_sha256"),
            "stderr_sha256": replay.get("stderr_sha256"),
            "failure_kind": "bound_semantic_assertion_failed",
            "matched_assertion_ast_sha256": sorted(
                str(assertion["assertion_ast_sha256"]) for assertion in assertions
            ),
        },
        "postconditions": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
    }
    contract["positive_outcome_contract_id"] = _content_addressed_receipt_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    return contract


def _json_scalar(value: Any) -> bool:
    if isinstance(value, (dict, list, tuple, set)):
        return False
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return value is None or isinstance(value, (str, int, float, bool))


def _semantic_basis_receipt(
    *,
    experiment: Mapping[str, Any],
    expected_value: Any,
    evidence_assignment: Mapping[str, Any],
    planning_workspace: Path | None,
    inspected_file_receipts: Sequence[Mapping[str, Any]],
    inspected_symbol_receipts: Sequence[Mapping[str, Any]],
    falsification_interventions: Sequence[Mapping[str, Any]],
    hypothesis_ids: set[str],
    mechanism_symbols: set[str],
) -> dict[str, Any] | None:
    """Provenance-check one research judgment about the desired exact value."""

    declared = experiment.get("positive_outcome_contract")
    if not isinstance(declared, dict) or declared.get("contract_kind") != (
        "retained_harness_semantic_assertion"
    ):
        return None
    rationale = _text(declared.get("semantic_rationale"))
    semantic_relation = declared.get("semantic_relation")
    basis = declared.get("semantic_basis")
    if (
        rationale is None
        or len(rationale) < 20
        or semantic_relation
        not in {
            "exact_expected_value",
            "logical_correction_of_source_failure",
            "required_operational_property",
            "repository_contract_requirement",
        }
        or not isinstance(basis, dict)
        or not _json_scalar(expected_value)
        or declared.get("expected_value") != expected_value
    ):
        return None
    exact_quote = _text(basis.get("exact_quote"))
    if exact_quote is None or not _expectation_quote(
        exact_quote,
        expected_value=expected_value,
    ):
        return None
    provenance: dict[str, Any]
    basis_kind = basis.get("kind")
    if basis_kind == "source_atom_quote":
        atom_id = _text(basis.get("atom_id"))
        field_path = _text(basis.get("field_path"))
        addressed = {
            atom_id
            for atom_id in experiment.get("addresses_atom_ids", [])
            if isinstance(atom_id, str)
        }
        atom_receipt = next(
            (
                receipt
                for receipt in evidence_assignment.get("atom_receipts", [])
                if isinstance(receipt, dict) and receipt.get("atom_id") == atom_id
            ),
            None,
        )
        snapshot = atom_receipt.get("atom_snapshot") if isinstance(atom_receipt, dict) else None
        found, field_value = _atom_field_path_value(snapshot or {}, field_path or "")
        if (
            atom_id is None
            or atom_id not in addressed
            or field_path is None
            or not _semantic_quote_field_path(field_path)
            or not _source_observation_atom(snapshot)
            or not found
            or not isinstance(field_value, str)
            or exact_quote not in field_value
        ):
            return None
        provenance = {
            "kind": "source_atom_quote",
            "verification_method": "runner_exact_source_atom_quote_v1",
            "atom_id": atom_id,
            "atom_sha256": atom_receipt.get("atom_sha256"),
            "field_path": field_path,
            "field_value_sha256": _canonical_json_sha256(field_value),
            "exact_quote": exact_quote,
            "exact_quote_sha256": sha256(exact_quote.encode("utf-8")).hexdigest(),
            "evidence_role": "observation",
        }
    elif basis_kind == "repository_contract_quote":
        if planning_workspace is None:
            return None
        path_raw = _text(basis.get("path"))
        contract_type = basis.get("contract_type")
        allowed_suffixes = {
            "api_contract": {".py", ".pyi"},
            "documentation": {".md", ".rst", ".txt"},
            "schema": {".json", ".toml", ".yaml", ".yml"},
        }
        relative = Path(path_raw or "")
        path = (planning_workspace / relative).resolve()
        inspected = next(
            (
                dict(receipt)
                for receipt in inspected_file_receipts
                if receipt.get("path") == relative.as_posix()
            ),
            None,
        )
        if (
            path_raw is None
            or contract_type not in allowed_suffixes
            or relative.is_absolute()
            or ".." in relative.parts
            or path.suffix.casefold() not in allowed_suffixes[str(contract_type)]
            or not _within(path, planning_workspace.resolve())
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
            symbol = _text(basis.get("symbol"))
            symbol_receipt = next(
                (
                    receipt
                    for receipt in inspected_symbol_receipts
                    if receipt.get("symbol") == symbol
                    and receipt.get("path") == relative.as_posix()
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
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                )
                and symbol is not None
                and symbol.replace(":", ".").replace("#", ".").endswith(node.name)
            ]
            segment = (
                ast.get_source_segment(content, candidates[0]) if len(candidates) == 1 else None
            )
            if (
                symbol not in mechanism_symbols
                or not isinstance(symbol_receipt, Mapping)
                or not isinstance(segment, str)
                or exact_quote not in segment
            ):
                return None
            locator = {"kind": "python_symbol", "symbol": symbol}
        elif contract_type == "schema":
            pointer = _text(basis.get("json_pointer"))
            exists, schema_value, _ = _config_value_for_symbol(
                path=path,
                symbol=f"config:{pointer or ''}",
            )
            if pointer is None or not pointer.startswith("/") or not exists:
                return None
            locator = {
                "kind": "schema_pointer",
                "json_pointer": pointer,
                "value_sha256": _canonical_json_sha256(schema_value),
            }
        else:
            subject = _text(basis.get("contract_subject"))
            allowed_subjects = mechanism_symbols | {
                symbol.rsplit(".", 1)[-1] for symbol in mechanism_symbols
            }
            if subject not in allowed_subjects or subject not in exact_quote:
                return None
            locator = {"kind": "mechanism_subject", "subject": subject}
        provenance = {
            "kind": "repository_contract_quote",
            "verification_method": "runner_researched_repository_contract_quote_v1",
            "contract_type": contract_type,
            "path": relative.as_posix(),
            "sha256": inspected.get("sha256"),
            "git_blob_sha": inspected.get("git_blob_sha"),
            "read_event_sha256": inspected.get("read_event_sha256"),
            "exact_quote": exact_quote,
            "exact_quote_sha256": sha256(exact_quote.encode("utf-8")).hexdigest(),
            "contract_locator": locator,
        }
    else:
        # A metamorphic/invariant basis will be added only when the runner has an
        # actual input-equivalence receipt.  Model-authored control prose is not it.
        return None

    relevant_interventions = [
        dict(intervention)
        for intervention in falsification_interventions
        if intervention.get("hypothesis_id") in hypothesis_ids
        and (
            intervention.get("baseline_experiment_id") == experiment.get("experiment_id")
            or intervention.get("support_experiment_id") == experiment.get("experiment_id")
        )
    ]
    review_ref = _text(declared.get("adversarial_review_reference"))
    adversarial_basis = next(
        (
            {
                "attempt_id": intervention.get("attempt_id"),
                "intervention_receipt_id": intervention.get("intervention_receipt_id"),
            }
            for intervention in relevant_interventions
            if intervention.get("attempt_id") == review_ref
        ),
        None,
    )
    if relevant_interventions and adversarial_basis is None:
        return None
    return {
        "schema_version": 1,
        "expected_value": expected_value,
        "expected_value_sha256": _canonical_json_sha256(expected_value),
        "semantic_rationale": rationale,
        "semantic_relation": semantic_relation,
        "semantic_judgment": "researcher_interpreted_grounded_expectation",
        "provenance": provenance,
        "adversarial_basis": adversarial_basis,
        "independent_review_requirement": "stage5_solution_falsification",
    }


def _retained_harness_positive_contract(
    *,
    experiment_id: str,
    experiment: dict[str, Any],
    replay: dict[str, Any],
    evidence: list[dict[str, Any]],
    evidence_assignment: Mapping[str, Any],
    planning_workspace: Path | None,
    inspected_file_receipts: Sequence[Mapping[str, Any]],
    inspected_symbol_receipts: Sequence[Mapping[str, Any]],
    falsification_interventions: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Mint a fail-first semantic contract from a retained research assertion."""

    harness_evidence = [
        item
        for item in evidence
        if item.get("evidence_type") == "temporary_harness"
        and _text(item.get("harness_path")) is not None
    ]
    harness_path = (
        _research_harness_relative_path(replay.get("executed_argv")) if harness_evidence else None
    )
    workspace_raw = _text(replay.get("workspace_dir"))
    exit_code = replay.get("exit_code")
    assertion = experiment.get("observable_assertion")
    if (
        harness_path is None
        or workspace_raw is None
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code == 0
        or not isinstance(assertion, dict)
        or assertion.get("source") != "exit_code"
        or assertion.get("operator") != "equals"
        or assertion.get("expected") != exit_code
    ):
        return None
    workspace = Path(workspace_raw).resolve()
    path = (workspace / harness_path).resolve()
    if not _within(path, workspace) or not path.is_file() or path.is_symlink():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(content)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    stderr_raw = _text(replay.get("stderr_path"))
    stderr_path = Path(stderr_raw) if stderr_raw is not None else None
    if stderr_path is None or not stderr_path.is_file():
        return None
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    if "AssertionError" not in stderr:
        return None
    aliases = _module_import_aliases(tree)
    symbol_paths = {
        str(point.get("symbol")): str(point.get("path"))
        for item in harness_evidence
        for point in item.get("code_paths", [])
        if isinstance(point, dict)
        and _text(point.get("symbol")) is not None
        and _text(point.get("path")) is not None
    }
    mechanism_symbols = sorted(
        {
            symbol
            for item in harness_evidence
            for symbol in item.get("mechanism_symbols", [])
            if isinstance(symbol, str) and symbol in symbol_paths
        }
    )
    targets_by_symbol = {
        symbol: _mechanism_call_targets(
            symbol=symbol,
            source_path=symbol_paths[symbol],
        )
        for symbol in mechanism_symbols
    }

    def expression_symbols(node: ast.AST) -> set[str]:
        observed: set[str] = set()
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Call):
                continue
            expression = _dotted_expression(candidate.func)
            if expression is None:
                continue
            resolved = _resolved_call_expression(expression, aliases)
            for symbol, targets in targets_by_symbol.items():
                if expression in targets or resolved in targets:
                    observed.add(symbol)
        return observed

    assigned_symbols: dict[str, set[str]] = {}
    for assignment in ast.walk(tree):
        if isinstance(assignment, ast.Assign):
            value = assignment.value
            targets = assignment.targets
        elif isinstance(assignment, ast.AnnAssign) and assignment.value is not None:
            value = assignment.value
            targets = [assignment.target]
        else:
            continue
        symbols = expression_symbols(value)
        for target in targets:
            if isinstance(target, ast.Name) and symbols:
                assigned_symbols.setdefault(target.id, set()).update(symbols)
    declared = experiment.get("positive_outcome_contract")
    expected_value = declared.get("expected_value") if isinstance(declared, dict) else object()
    hypothesis_ids = {
        str(item.get("hypothesis_id"))
        for item in harness_evidence
        if _text(item.get("hypothesis_id")) is not None
    }
    evidence_mechanism_symbols = {
        symbol
        for item in harness_evidence
        for symbol in item.get("mechanism_symbols", [])
        if isinstance(symbol, str)
    }
    semantic_basis = _semantic_basis_receipt(
        experiment=experiment,
        expected_value=expected_value,
        evidence_assignment=evidence_assignment,
        planning_workspace=planning_workspace,
        inspected_file_receipts=inspected_file_receipts,
        inspected_symbol_receipts=inspected_symbol_receipts,
        falsification_interventions=falsification_interventions,
        hypothesis_ids=hypothesis_ids,
        mechanism_symbols=evidence_mechanism_symbols,
    )
    if semantic_basis is None:
        return None

    def dependent_symbols(node: ast.AST) -> set[str]:
        symbols = expression_symbols(node)
        for loaded in (
            candidate
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Load)
        ):
            symbols.update(assigned_symbols.get(loaded.id, set()))
        return symbols

    assertions: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        if (
            not isinstance(node.test, ast.Compare)
            or len(node.test.ops) != 1
            or not isinstance(node.test.ops[0], (ast.Eq, ast.Is))
            or len(node.test.comparators) != 1
        ):
            continue
        left = node.test.left
        right = node.test.comparators[0]
        left_symbols = dependent_symbols(left)
        right_symbols = dependent_symbols(right)
        if bool(left_symbols) == bool(right_symbols):
            continue
        expected_node = right if left_symbols else left
        symbols = left_symbols or right_symbols
        try:
            observed_expected = ast.literal_eval(expected_node)
        except (ValueError, TypeError):
            continue
        if (
            observed_expected != expected_value
            or not _json_scalar(observed_expected)
            or not re.search(rf"\bline\s+{node.lineno}\b", stderr)
        ):
            continue
        projection = ast.dump(
            node.test,
            annotate_fields=True,
            include_attributes=False,
        )
        assertions.append(
            {
                "line": node.lineno,
                "expression": ast.get_source_segment(content, node.test) or ast.unparse(node.test),
                "assertion_ast_sha256": sha256(projection.encode("utf-8")).hexdigest(),
                "mechanism_symbols": sorted(symbols),
                "expected_value_sha256": _canonical_json_sha256(observed_expected),
            }
        )
    if not assertions:
        return None
    contract: dict[str, Any] = {
        "schema_version": 1,
        "kind": "retained_research_harness_assertion",
        "research_experiment_id": experiment_id,
        "mechanism_evidence_ids": sorted(
            {
                str(item["mechanism_evidence_id"])
                for item in harness_evidence
                if _text(item.get("mechanism_evidence_id")) is not None
            }
        ),
        "research_assertion_contract": {
            "harness_path": harness_path,
            "harness_sha256": _sha256_path(path),
            "semantic_assertions": assertions,
            "baseline_failure": {
                "exit_code": exit_code,
                "stderr_sha256": replay.get("stderr_sha256"),
                "failure_kind": "semantic_assertion_failed",
            },
        },
        "semantic_basis": semantic_basis,
        "postconditions": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
    }
    contract["positive_outcome_contract_id"] = _content_addressed_receipt_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    return contract


def _origin_semantic_positive_contract(
    *,
    experiment_id: str,
    experiment: dict[str, Any],
    evidence: list[dict[str, Any]],
    evidence_assignment: Mapping[str, Any],
    atom_bindings: list[dict[str, Any]],
    oracle_kind: str,
    state_targets: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any] | None:
    """Mint an exact postcondition explicitly supplied by immutable origin evidence."""

    declared_raw = experiment.get("positive_outcome_contract")
    if declared_raw is None:
        return None
    declared = declared_raw if isinstance(declared_raw, dict) else {}
    if declared.get("contract_kind") != "origin_atom_exact_value":
        return None
    atom_id = _text(declared.get("atom_id"))
    field_path = _text(declared.get("field_path"))
    postcondition_raw = declared.get("postcondition")
    postcondition = postcondition_raw if isinstance(postcondition_raw, dict) else {}
    atom_receipt = next(
        (
            item
            for item in evidence_assignment.get("atom_receipts", [])
            if isinstance(item, dict) and item.get("atom_id") == atom_id
        ),
        None,
    )
    expected_behavior_binding = next(
        (
            binding
            for binding in atom_bindings
            if isinstance(binding, dict)
            and binding.get("experiment_id") == experiment_id
            and binding.get("atom_id") == atom_id
            and binding.get("binding_role") == "expected_behavior"
            and binding.get("origin_atom_field_path") == field_path
        ),
        None,
    )
    found, evidence_value = (
        _atom_field_path_value(
            atom_receipt.get("atom_snapshot", {}),
            field_path or "",
        )
        if isinstance(atom_receipt, dict)
        else (False, None)
    )
    predicate_type = _text(postcondition.get("type"))
    grounded_predicate: dict[str, Any] | None = None
    declared_value: Any = None
    if predicate_type in {
        "command_stdout_equals",
        "command_stdout_contains",
        "command_stderr_equals",
        "command_stderr_contains",
        "command_combined_equals",
        "command_combined_contains",
    }:
        declared_value = postcondition.get("value")
        if oracle_kind == "staged_replay" and declared_value == evidence_value:
            grounded_predicate = {
                "type": predicate_type,
                "command_index": 0,
                "value": declared_value,
            }
    elif predicate_type == "artifact_json_value":
        declared_value = postcondition.get("equals")
        path = _text(postcondition.get("path"))
        pointer = postcondition.get("json_pointer")
        if (
            oracle_kind == "staged_replay"
            and declared_value == evidence_value
            and path is not None
            and not path.startswith(("/", "\\"))
            and ".." not in path.replace("\\", "/").split("/")
            and isinstance(pointer, str)
            and (not pointer or pointer.startswith("/"))
        ):
            grounded_predicate = {
                "type": predicate_type,
                "path": path,
                "json_pointer": pointer,
                "equals": declared_value,
            }
    elif predicate_type == "config_state_equals":
        declared_value = postcondition.get("equals")
        symbol = _text(postcondition.get("mechanism_symbol"))
        pointer = "/" + (symbol or "").removeprefix("config:/")
        target = next(
            (
                item
                for item in state_targets
                if isinstance(item, dict) and item.get("json_pointer") == pointer
            ),
            None,
        )
        if (
            oracle_kind == "config_state"
            and declared_value == evidence_value
            and symbol is not None
            and isinstance(target, dict)
        ):
            grounded_predicate = {
                "type": "oracle_state_equals",
                "target_id": target.get("target_id"),
                "exists": postcondition.get("exists") is not False,
                "equals": declared_value,
            }
    if (
        declared.get("contract_kind") != "origin_atom_exact_value"
        or atom_id is None
        or field_path is None
        or not _expected_semantic_field_path(field_path)
        or not isinstance(expected_behavior_binding, dict)
        or not found
        or not _source_observation_atom(
            atom_receipt.get("atom_snapshot") if isinstance(atom_receipt, dict) else None
        )
        or grounded_predicate is None
    ):
        errors.append(f"positive_outcome_semantic_contract_unverified:{experiment_id}")
        return None
    postconditions = [grounded_predicate]
    if oracle_kind == "staged_replay":
        postconditions.insert(
            0,
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
        )
    contract = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": experiment_id,
        "mechanism_evidence_ids": sorted(
            {
                str(item["mechanism_evidence_id"])
                for item in evidence
                if _text(item.get("mechanism_evidence_id")) is not None
            }
        ),
        "origin_evidence": {
            "atom_id": atom_id,
            "atom_sha256": atom_receipt.get("atom_sha256"),
            "field_path": field_path,
            "value_sha256": _canonical_json_sha256(evidence_value),
        },
        "postconditions": postconditions,
    }
    contract["positive_outcome_contract_id"] = _content_addressed_receipt_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    return contract


def _causal_proof_positive_contract(
    *,
    experiment_id: str,
    evidence: Sequence[Mapping[str, Any]],
    proof_receipt: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Retain an adapter-neutral, executable definition of a positive result."""

    if validate_causal_proof_receipt(proof_receipt):
        return None
    intervention = proof_receipt.get("intervention")
    observations = proof_receipt.get("observations")
    baseline = observations.get("baseline") if isinstance(observations, Mapping) else None
    challenge = observations.get("challenge") if isinstance(observations, Mapping) else None
    positive = proof_receipt.get("positive_outcome")
    source_root = proof_receipt.get("source_root")
    positive_basis = (
        source_root.get("positive_basis") if isinstance(source_root, Mapping) else None
    )
    if (
        not isinstance(intervention, Mapping)
        or intervention.get("baseline_experiment_id") != experiment_id
        or not isinstance(baseline, Mapping)
        or not isinstance(challenge, Mapping)
        or not isinstance(positive, Mapping)
        or not isinstance(positive_basis, Mapping)
    ):
        return None
    proof_id = _text(proof_receipt.get("proof_receipt_id"))
    intervention_id = _text(proof_receipt.get("intervention_id"))
    adapter_id = _text(proof_receipt.get("adapter_id"))
    adapter_version = _text(proof_receipt.get("adapter_version"))
    if None in {proof_id, intervention_id, adapter_id, adapter_version}:
        return None
    mechanism_ids = sorted(
        {
            str(item["mechanism_evidence_id"])
            for item in evidence
            if item.get("proof_receipt_id") == proof_id
            and _text(item.get("mechanism_evidence_id")) is not None
        }
    )
    if not mechanism_ids:
        return None
    postcondition = {
        "type": "causal_proof_predicate",
        "proof_receipt_id": proof_id,
        "intervention_id": intervention_id,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "predicate": positive.get("predicate"),
        "observation_source": positive.get("observation_source"),
        "positive_basis_sha256": positive_basis.get("basis_sha256"),
    }
    contract: dict[str, Any] = {
        "schema_version": 1,
        "kind": "causal_proof_predicate",
        "research_experiment_id": experiment_id,
        "mechanism_evidence_ids": mechanism_ids,
        "proof_receipt_id": proof_id,
        "intervention_id": intervention_id,
        "adapter_contract": {
            "adapter_id": adapter_id,
            "adapter_version": adapter_version,
            "baseline_observation_sha256": baseline.get("observation_sha256"),
            "challenge_observation_sha256": challenge.get("observation_sha256"),
            "adapter_evidence_sha256": _canonical_json_sha256(
                proof_receipt.get("adapter_evidence")
            ),
        },
        "positive_basis": dict(positive_basis),
        "semantic_review_required": positive_basis.get("semantic_review_required") is True,
        "postconditions": [postcondition],
    }
    contract["positive_outcome_contract_id"] = _content_addressed_receipt_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    return contract


def _positive_outcome_contracts(
    *,
    experiment_id: str,
    experiment: dict[str, Any],
    replay: dict[str, Any],
    evidence: list[dict[str, Any]],
    experiments: Mapping[str, dict[str, Any]],
    clean_replays: Mapping[str, dict[str, Any]],
    control_verifications: list[dict[str, Any]],
    falsification_interventions: list[dict[str, Any]],
    inspected_file_receipts: list[dict[str, Any]],
    inspected_symbol_receipts: list[dict[str, Any]],
    evidence_assignment: Mapping[str, Any],
    atom_bindings: list[dict[str, Any]],
    planning_workspace: Path | None,
    oracle_kind: str,
    state_targets: list[dict[str, Any]],
    proof_adapter_receipts: Sequence[Mapping[str, Any]] = (),
    primary_hypothesis_id: str,
    primary_verified_mechanism_sha256: str,
    primary_verified_mechanism_provenance_sha256: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for proof_receipt in proof_adapter_receipts:
        generic_contract = _causal_proof_positive_contract(
            experiment_id=experiment_id,
            evidence=evidence,
            proof_receipt=proof_receipt,
        )
        if generic_contract is not None:
            contracts.append(generic_contract)
    repository_contract = _repository_test_positive_contract(
        experiment_id=experiment_id,
        experiment=experiment,
        replay=replay,
        evidence=evidence,
        evidence_assignment=evidence_assignment,
        atom_bindings=atom_bindings,
        planning_workspace=planning_workspace,
    )
    if repository_contract is not None:
        contracts.append(repository_contract)
    harness_contract = _retained_harness_positive_contract(
        experiment_id=experiment_id,
        experiment=experiment,
        replay=replay,
        evidence=evidence,
        evidence_assignment=evidence_assignment,
        planning_workspace=planning_workspace,
        inspected_file_receipts=inspected_file_receipts,
        inspected_symbol_receipts=inspected_symbol_receipts,
        falsification_interventions=falsification_interventions,
    )
    if harness_contract is not None:
        contracts.append(harness_contract)
    # A different control input cannot establish the original input's desired
    # value without a runner-attested equivalence/invariant receipt.  Existing
    # controlled differences remain causal boundary evidence, not standalone
    # positive outcome contracts.
    del experiments, clean_replays, control_verifications
    semantic_contract = _origin_semantic_positive_contract(
        experiment_id=experiment_id,
        experiment=experiment,
        evidence=evidence,
        evidence_assignment=evidence_assignment,
        atom_bindings=atom_bindings,
        oracle_kind=oracle_kind,
        state_targets=state_targets,
        errors=errors,
    )
    if semantic_contract is not None:
        contracts.append(semantic_contract)
    for contract in contracts:
        contract["primary_hypothesis_id"] = primary_hypothesis_id
        contract["primary_verified_mechanism_sha256"] = primary_verified_mechanism_sha256
        contract["primary_verified_mechanism_provenance_sha256"] = (
            primary_verified_mechanism_provenance_sha256
        )
        contract["positive_outcome_contract_id"] = _content_addressed_receipt_id(
            "positive_outcome_contract",
            contract,
            "positive_outcome_contract_id",
        )
    by_id = {str(contract["positive_outcome_contract_id"]): contract for contract in contracts}
    return [by_id[key] for key in sorted(by_id)]


def _exact_original_replay_observation(
    *,
    experiment_id: str,
    replay: Mapping[str, Any],
    positive_outcome_contracts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Mint a portable post-change selector for an exact original command oracle."""

    matching_postconditions = [
        postcondition
        for contract in positive_outcome_contracts
        for postcondition in (
            contract.get("postconditions", [])
            if isinstance(contract.get("postconditions"), list)
            else []
        )
        if isinstance(postcondition, Mapping)
        and postcondition.get("type") == "command_exit_code"
        and postcondition.get("command_index") == 0
        and isinstance(postcondition.get("equals"), int)
        and not isinstance(postcondition.get("equals"), bool)
    ]
    exit_code = replay.get("exit_code")
    if (
        len(matching_postconditions) != 1
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
    ):
        return None
    contract_ids = sorted(
        {
            str(contract.get("positive_outcome_contract_id"))
            for contract in positive_outcome_contracts
            if _text(contract.get("positive_outcome_contract_id")) is not None
        }
    )
    if not contract_ids:
        return None
    return content_bound_payload(
        {
            "schema_version": 1,
            "source_experiment_id": experiment_id,
            "selector": {"source": "exit_code"},
            "source_observation_sha256": _canonical_json_sha256(
                {
                    "exit_code": exit_code,
                    "stdout_sha256": replay.get("stdout_sha256"),
                    "stderr_sha256": replay.get("stderr_sha256"),
                }
            ),
            "predicate_input_mode": "post_change_observation",
            "positive_outcome_contract_ids": contract_ids,
            "runner_attested": True,
        },
        hash_field="replay_observation_sha256",
    )


def _outcome_oracle_receipts(
    dossier: dict[str, Any],
    *,
    clean_replays: dict[str, dict[str, Any]],
    mechanism_evidence: list[dict[str, Any]],
    proof_adapter_receipts: Sequence[Mapping[str, Any]] = (),
    verified_mechanism: dict[str, Any] | None,
    verified_mechanism_sha256: str | None,
    verified_mechanism_provenance: dict[str, Any] | None,
    verified_mechanism_provenance_sha256: str | None,
    control_verifications: list[dict[str, Any]],
    falsification_interventions: list[dict[str, Any]],
    inspected_file_receipts: list[dict[str, Any]],
    inspected_symbol_receipts: list[dict[str, Any]],
    evidence_assignment: Mapping[str, Any],
    atom_bindings: list[dict[str, Any]],
    planning_workspace: Path | None,
    research_workspace: Path | None,
    overlay_manifest: dict[str, Any],
    run_dir: Path,
    repo_revision: str | None,
    errors: list[str],
) -> list[dict[str, Any]]:
    """Promote verified research evidence into executable post-merge oracles.

    This is deliberately a positive path for evidence that already passed the
    research gate.  It does not infer broader PR scope or turn arbitrary static
    inspection into behavior.
    """

    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    primary = hypotheses[0] if hypotheses and isinstance(hypotheses[0], dict) else {}
    primary_hypothesis_id = _text(primary.get("hypothesis_id"))
    selected_evidence_ids = (
        {
            value
            for value in verified_mechanism_provenance.get("mechanism_evidence_ids", [])
            if isinstance(value, str) and value
        }
        if isinstance(verified_mechanism_provenance, dict)
        else set()
    )
    if (
        primary_hypothesis_id is None
        or not isinstance(verified_mechanism, dict)
        or verified_mechanism_sha256 != _canonical_json_sha256(verified_mechanism)
        or not isinstance(verified_mechanism_provenance, dict)
        or verified_mechanism_provenance_sha256
        != _canonical_json_sha256(verified_mechanism_provenance)
        or verified_mechanism_provenance.get("primary_hypothesis_id") != primary_hypothesis_id
        or not selected_evidence_ids
    ):
        return []

    experiments_raw = dossier.get("experiments")
    experiments = {
        str(item.get("experiment_id")): item
        for item in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(item, dict) and _text(item.get("experiment_id")) is not None
    }
    evidence_by_experiment: dict[str, list[dict[str, Any]]] = {}
    for evidence in mechanism_evidence:
        if not isinstance(evidence, dict):
            continue
        evidence_id = _text(evidence.get("mechanism_evidence_id"))
        if (
            evidence.get("hypothesis_id") != primary_hypothesis_id
            or evidence.get("adversarial_effect") != "supports_selection"
            or evidence_id not in selected_evidence_ids
        ):
            continue
        for experiment_id in evidence.get("experiment_ids", []):
            if isinstance(experiment_id, str):
                evidence_by_experiment.setdefault(experiment_id, []).append(evidence)
    proofs_by_baseline: dict[str, list[Mapping[str, Any]]] = {}
    for proof in proof_adapter_receipts:
        if validate_causal_proof_receipt(proof):
            continue
        intervention = proof.get("intervention")
        baseline_id = (
            _text(intervention.get("baseline_experiment_id"))
            if isinstance(intervention, Mapping)
            else None
        )
        if baseline_id is not None:
            proofs_by_baseline.setdefault(baseline_id, []).append(proof)

    retained_asset: dict[str, Any] | None = None
    receipts: list[dict[str, Any]] = []
    for experiment_id, experiment in sorted(experiments.items()):
        replay = clean_replays.get(experiment_id)
        evidence = evidence_by_experiment.get(experiment_id, [])
        if (
            not isinstance(replay, dict)
            or experiment.get("outcome") != "supports"
            or replay.get("assertion_passed") is not True
            or not evidence
            or repo_revision is None
        ):
            continue
        scenario_kind = str(experiment.get("scenario_kind") or "")
        experiment_proofs = proofs_by_baseline.get(experiment_id, [])
        mechanism_ids = sorted(
            {
                str(item["mechanism_evidence_id"])
                for item in evidence
                if _text(item.get("mechanism_evidence_id")) is not None
            }
        )
        origin_atom_ids = sorted(
            {
                str(atom_id)
                for item in evidence
                for atom_id in item.get("origin_atom_ids", [])
                if isinstance(atom_id, str) and atom_id.strip()
            }
        )
        common: dict[str, Any] = {
            "schema_version": 1,
            "case_id": dossier.get("case_id"),
            "repo_revision": repo_revision,
            "primary_hypothesis_id": primary_hypothesis_id,
            "primary_verified_mechanism_sha256": verified_mechanism_sha256,
            "primary_verified_mechanism_provenance_sha256": (verified_mechanism_provenance_sha256),
            "research_experiment_id": experiment_id,
            "scenario_kind": scenario_kind,
            "origin_atom_ids": origin_atom_ids,
            "mechanism_evidence_ids": mechanism_ids,
            "baseline": {
                "exit_code": replay.get("exit_code"),
                "observable_assertion": experiment.get("observable_assertion"),
                "stdout_sha256": replay.get("stdout_sha256"),
                "stderr_sha256": replay.get("stderr_sha256"),
            },
        }
        if experiment_proofs:
            argv = replay.get("executed_argv")
            authorization = replay.get("command_authorization")
            setup_receipt = replay.get("replay_setup_receipt")
            replay_inputs = replay.get("replay_inputs")
            proof_replay_observations = [
                proof.get("replay_observation")
                for proof in experiment_proofs
                if isinstance(proof, Mapping)
            ]
            replay_observation = (
                proof_replay_observations[0] if proof_replay_observations else None
            )
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(token, str) or not token for token in argv)
                or not _command_authorization_attested(authorization, argv=argv)
                or not isinstance(setup_receipt, dict)
                or setup_receipt.get("runner_applied") is not True
                or setup_receipt.get("replay_setup_sha256")
                != _canonical_json_sha256(
                    {
                        key: value
                        for key, value in setup_receipt.items()
                        if key != "replay_setup_sha256"
                    }
                )
                or not _replay_inputs_attested(
                    replay_inputs,
                    experiment_id=experiment_id,
                )
                or not isinstance(replay_observation, Mapping)
                or any(
                    observation != replay_observation
                    for observation in proof_replay_observations
                )
            ):
                continue
            proof_ids = sorted(
                str(proof["proof_receipt_id"])
                for proof in experiment_proofs
                if _text(proof.get("proof_receipt_id")) is not None
            )
            needs_asset = any(
                token.replace("\\", "/").startswith(".usertest_research/")
                for token in argv
            )
            asset: dict[str, Any] | None = None
            if needs_asset:
                if retained_asset is None and research_workspace is not None:
                    retained_asset = _persist_outcome_overlay_asset(
                        run_dir=run_dir,
                        research_workspace=research_workspace,
                        overlay_manifest=overlay_manifest,
                        errors=errors,
                    )
                asset = retained_asset
                if asset is None:
                    continue
            receipt = {
                **common,
                "kind": "causal_proof_replay",
                "proof_scope": "adapter_causal_behavior",
                "proof_receipt_ids": proof_ids,
                "execution": {
                    "argv": list(argv),
                    "command_authorization": dict(authorization),
                    "platform_requirement": experiment.get("platform_requirement", "any"),
                    "shell": False,
                    "replay_setup_receipt": dict(setup_receipt),
                    "replay_inputs": dict(replay_inputs),
                    "replay_observation": dict(replay_observation),
                    "replay_setup_reference": {
                        "source": "research_experiment",
                        "experiment_id": experiment_id,
                        "replay_setup_sha256": _canonical_json_sha256(
                            experiment.get("replay_setup", {})
                        ),
                    },
                },
                "asset": asset,
            }
        elif scenario_kind == "static_trace":
            config_evidence = [
                item
                for item in evidence
                if item.get("evidence_type") == "static_trace"
                and isinstance(item.get("mechanism_symbols"), list)
                and item.get("mechanism_symbols")
                and all(
                    isinstance(symbol, str) and symbol.startswith("config:/")
                    for symbol in item.get("mechanism_symbols", [])
                )
            ]
            if (
                not config_evidence
                or len(config_evidence) != len(evidence)
                or planning_workspace is None
            ):
                continue
            targets: dict[str, dict[str, Any]] = {}
            for item in config_evidence:
                paths = {
                    str(row.get("symbol")): str(row.get("path"))
                    for row in item.get("code_paths", [])
                    if isinstance(row, dict)
                    and _text(row.get("symbol")) is not None
                    and _text(row.get("path")) is not None
                }
                for symbol in item.get("mechanism_symbols", []):
                    relative_path = paths.get(str(symbol))
                    if relative_path is None:
                        continue
                    source = (planning_workspace / relative_path).resolve()
                    exists, value, format_name = _config_value_for_symbol(
                        path=source,
                        symbol=str(symbol),
                    )
                    if not exists or format_name is None:
                        errors.append(
                            f"outcome_oracle_config_value_unavailable:{experiment_id}:{symbol}"
                        )
                        continue
                    pointer = "/" + str(symbol).removeprefix("config:/")
                    target = {
                        "path": PurePosixPath(relative_path.replace("\\", "/")).as_posix(),
                        "format": format_name,
                        "json_pointer": pointer,
                        "source_file_sha256": _sha256_path(source),
                        "baseline_exists": True,
                        "baseline_value": value,
                        "baseline_value_sha256": _canonical_json_sha256(value),
                    }
                    target["target_id"] = _content_addressed_receipt_id(
                        "config_state",
                        target,
                        "target_id",
                    )
                    targets[str(target["target_id"])] = target
            if not targets:
                continue
            receipt = {
                **common,
                "kind": "config_state",
                "proof_scope": "configuration_state",
                "state_targets": [targets[key] for key in sorted(targets)],
            }
        elif scenario_kind in {"original_replay", "faithful_replay", "live_runtime"}:
            argv = replay.get("executed_argv")
            authorization = replay.get("command_authorization")
            replay_inputs = replay.get("replay_inputs")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(token, str) or not token for token in argv)
                or not _command_authorization_attested(authorization, argv=argv)
            ):
                continue
            needs_asset = any(
                token.replace("\\", "/").startswith(".usertest_research/") for token in argv
            ) or any(_text(item.get("harness_path")) is not None for item in evidence)
            asset: dict[str, Any] | None = None
            if needs_asset:
                if retained_asset is None and research_workspace is not None:
                    retained_asset = _persist_outcome_overlay_asset(
                        run_dir=run_dir,
                        research_workspace=research_workspace,
                        overlay_manifest=overlay_manifest,
                        errors=errors,
                    )
                asset = retained_asset
                if asset is None:
                    continue
            receipt = {
                **common,
                "kind": "staged_replay",
                "proof_scope": "behavioral",
                "execution": {
                    "argv": list(argv),
                    "command_authorization": dict(authorization),
                    "platform_requirement": experiment.get("platform_requirement", "any"),
                    "shell": False,
                    **(
                        {"replay_inputs": dict(replay_inputs)}
                        if _replay_inputs_attested(
                            replay_inputs,
                            experiment_id=experiment_id,
                        )
                        else {}
                    ),
                },
                "asset": asset,
            }
        else:
            continue
        positive_contracts = _positive_outcome_contracts(
            experiment_id=experiment_id,
            experiment=experiment,
            replay=replay,
            evidence=evidence,
            experiments=experiments,
            clean_replays=clean_replays,
            control_verifications=control_verifications,
            falsification_interventions=falsification_interventions,
            inspected_file_receipts=inspected_file_receipts,
            inspected_symbol_receipts=inspected_symbol_receipts,
            evidence_assignment=evidence_assignment,
            atom_bindings=atom_bindings,
            planning_workspace=planning_workspace,
            oracle_kind=str(receipt.get("kind") or ""),
            state_targets=[
                item
                for item in (
                    receipt.get("state_targets")
                    if isinstance(receipt.get("state_targets"), list)
                    else []
                )
                if isinstance(item, dict)
            ],
            proof_adapter_receipts=experiment_proofs,
            primary_hypothesis_id=primary_hypothesis_id,
            primary_verified_mechanism_sha256=verified_mechanism_sha256,
            primary_verified_mechanism_provenance_sha256=(verified_mechanism_provenance_sha256),
            errors=errors,
        )
        receipt["positive_outcome_contracts"] = positive_contracts
        if receipt.get("kind") == "staged_replay":
            replay_observation = _exact_original_replay_observation(
                experiment_id=experiment_id,
                replay=replay,
                positive_outcome_contracts=positive_contracts,
            )
            execution = receipt.get("execution")
            if replay_observation is not None and isinstance(execution, dict):
                execution["replay_observation"] = replay_observation
        receipt["outcome_oracle_id"] = _content_addressed_receipt_id(
            "outcome_oracle",
            receipt,
            "outcome_oracle_id",
        )
        receipts.append(receipt)
    receipts.sort(key=lambda item: str(item.get("outcome_oracle_id")))
    return receipts


def _verification_boundary_receipts(
    *,
    experiments: Mapping[str, Mapping[str, Any]],
    clean_replays: Mapping[str, Mapping[str, Any]],
    mechanism_evidence: Sequence[Mapping[str, Any]],
    proof_adapter_receipts: Sequence[Mapping[str, Any]],
    outcome_oracles: Sequence[Mapping[str, Any]],
    verified_mechanism_provenance: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind model-authored verification scope to runner-owned evidence.

    The semantic labels remain open.  A model may conservatively require live
    verification from any attested replay; it may waive live verification only
    when it explicitly claims faithful equivalence and the runner can bind that
    claim to both the selected mechanism and an executable outcome oracle.
    """

    selected_ids = {
        str(item)
        for item in (
            verified_mechanism_provenance.get("mechanism_evidence_ids", [])
            if isinstance(verified_mechanism_provenance, Mapping)
            else []
        )
        if isinstance(item, str) and item
    }
    receipts: list[dict[str, Any]] = []
    errors: list[str] = []
    for experiment_id, experiment in experiments.items():
        declaration = experiment.get("verification_boundary")
        if declaration is None:
            continue
        label = f"verification_boundary_invalid:{experiment_id}"
        if not isinstance(declaration, Mapping):
            errors.append(label)
            continue
        boundary_kind = _text(declaration.get("boundary_kind"))
        rationale = _text(declaration.get("rationale"))
        requires_live = declaration.get("requires_live_verification")
        faithful = declaration.get("faithful_equivalence")
        replay = clean_replays.get(experiment_id)
        authorization = (
            replay.get("command_authorization") if isinstance(replay, Mapping) else None
        )
        argv = replay.get("executed_argv") if isinstance(replay, Mapping) else None
        if (
            boundary_kind is None
            or rationale is None
            or not isinstance(requires_live, bool)
            or not isinstance(faithful, bool)
            or not isinstance(replay, Mapping)
            or not isinstance(argv, list)
            or not _command_authorization_attested(authorization, argv=argv)
            or replay.get("assertion_passed") is not True
        ):
            errors.append(label)
            continue
        selected_mechanism_ids = sorted(
            {
                str(item.get("mechanism_evidence_id"))
                for item in mechanism_evidence
                if _text(item.get("mechanism_evidence_id")) is not None
                and item.get("mechanism_evidence_id") in selected_ids
                and experiment_id in item.get("experiment_ids", [])
            }
        )
        oracle_ids = sorted(
            {
                str(oracle.get("outcome_oracle_id"))
                for oracle in outcome_oracles
                if oracle.get("research_experiment_id") == experiment_id
                and _text(oracle.get("outcome_oracle_id")) is not None
                and oracle.get("outcome_oracle_id")
                == _content_addressed_receipt_id(
                    "outcome_oracle",
                    oracle,
                    "outcome_oracle_id",
                )
            }
        )
        proof_ids = sorted(
            {
                str(proof.get("proof_receipt_id"))
                for proof in proof_adapter_receipts
                if _text(proof.get("proof_receipt_id")) is not None
                and isinstance(proof.get("replay_observation"), Mapping)
                and proof["replay_observation"].get("source_experiment_id")
                == experiment_id
            }
        )
        matching_proofs = [
            proof
            for proof in proof_adapter_receipts
            if _text(proof.get("proof_receipt_id")) in proof_ids
        ]
        matching_oracles = [
            oracle
            for oracle in outcome_oracles
            if oracle.get("research_experiment_id") == experiment_id
            and _text(oracle.get("outcome_oracle_id")) in oracle_ids
            and oracle.get("outcome_oracle_id")
            == _content_addressed_receipt_id(
                "outcome_oracle",
                oracle,
                "outcome_oracle_id",
            )
        ]
        equivalence_proof: dict[str, Any] | None = None
        for proof in matching_proofs:
            source_root = proof.get("source_root")
            proof_inputs = proof.get("replay_inputs")
            proof_observation = proof.get("replay_observation")
            origin_atom_ids = sorted(
                {
                    str(atom_id)
                    for atom_id in (
                        source_root.get("origin_atom_ids", [])
                        if isinstance(source_root, Mapping)
                        else []
                    )
                    if isinstance(atom_id, str) and atom_id
                }
            )
            command_identity = bool(
                authorization.get("origin_atom_id") in origin_atom_ids
                and authorization.get("origin_atom_field_path") == "$.command"
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(authorization.get("origin_atom_sha256") or ""),
                )
                is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(authorization.get("origin_command_value_sha256") or ""),
                )
                is not None
            )
            predicate_bindings = [
                binding
                for binding in (
                    source_root.get("atom_field_predicate_bindings", [])
                    if isinstance(source_root, Mapping)
                    else []
                )
                if isinstance(binding, Mapping)
                and binding.get("runner_attested") is True
                and binding.get("baseline_experiment_id") == experiment_id
                and binding.get("atom_id") in origin_atom_ids
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(binding.get("atom_field_binding_sha256") or ""),
                )
                is not None
            ]
            source_identity_refs = (
                [f"command_authorization:{authorization.get('authorization_sha256')}"]
                if command_identity
                else sorted(
                    f"atom_field_binding:{binding['atom_field_binding_sha256']}"
                    for binding in predicate_bindings
                )
            )
            proof_id = _text(proof.get("proof_receipt_id"))
            replay_inputs = replay.get("replay_inputs")
            bound_oracle = next(
                (
                    oracle
                    for oracle in matching_oracles
                    if proof_id in oracle.get("proof_receipt_ids", [])
                    and isinstance(oracle.get("execution"), Mapping)
                    and oracle["execution"].get("replay_inputs") == proof_inputs
                    and oracle["execution"].get("replay_observation")
                    == proof_observation
                ),
                None,
            )
            if (
                not origin_atom_ids
                or not source_identity_refs
                or not selected_mechanism_ids
                or bound_oracle is None
                or not isinstance(source_root, Mapping)
                or not isinstance(proof_inputs, Mapping)
                or not isinstance(proof_observation, Mapping)
                or not isinstance(replay_inputs, Mapping)
                or proof_inputs.get("replay_inputs_sha256")
                != replay_inputs.get("replay_inputs_sha256")
                or proof_observation.get("source_experiment_id") != experiment_id
            ):
                continue
            equivalence_proof = content_bound_payload(
                {
                    "schema_version": 1,
                    "equivalence_mode": "causal_proof_source_identity",
                    "source_experiment_id": experiment_id,
                    "origin_atom_ids": origin_atom_ids,
                    "source_root_sha256": source_root.get("source_root_sha256"),
                    "source_identity_refs": source_identity_refs,
                    "proof_receipt_id": proof_id,
                    "replay_inputs_sha256": proof_inputs.get("replay_inputs_sha256"),
                    "replay_observation_sha256": proof_observation.get(
                        "replay_observation_sha256"
                    ),
                    "selected_mechanism_evidence_ids": selected_mechanism_ids,
                    "outcome_oracle_id": bound_oracle.get("outcome_oracle_id"),
                    "runner_attested": True,
                },
                hash_field="equivalence_sha256",
            )
            break
        if equivalence_proof is None:
            origin_atom_id = _text(authorization.get("origin_atom_id"))
            selected_origin_atom_ids = {
                str(atom_id)
                for item in mechanism_evidence
                if item.get("mechanism_evidence_id") in selected_mechanism_ids
                and experiment_id in item.get("experiment_ids", [])
                for atom_id in item.get("origin_atom_ids", [])
                if isinstance(atom_id, str) and atom_id
            }
            exact_source_identity = bool(
                origin_atom_id in selected_origin_atom_ids
                and authorization.get("origin_atom_field_path") == "$.command"
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(authorization.get("origin_atom_sha256") or ""),
                )
                is not None
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(authorization.get("origin_command_value_sha256") or ""),
                )
                is not None
            )
            replay_inputs = replay.get("replay_inputs")
            source_identity = (
                content_bound_payload(
                    {
                        "schema_version": 1,
                        "origin_atom_id": origin_atom_id,
                        "origin_atom_sha256": authorization.get("origin_atom_sha256"),
                        "origin_atom_field_path": "$.command",
                        "origin_command_value_sha256": authorization.get(
                            "origin_command_value_sha256"
                        ),
                        "executed_argv_sha256": authorization.get(
                            "executed_argv_sha256"
                        ),
                        "command_authorization_sha256": authorization.get(
                            "authorization_sha256"
                        ),
                        "runner_attested": True,
                    },
                    hash_field="source_identity_sha256",
                )
                if exact_source_identity
                else None
            )
            for oracle in matching_oracles:
                execution = oracle.get("execution")
                oracle_observation = (
                    execution.get("replay_observation")
                    if isinstance(execution, Mapping)
                    else None
                )
                positive_contracts = oracle.get("positive_outcome_contracts")
                positive_contract_ids = sorted(
                    {
                        str(contract.get("positive_outcome_contract_id"))
                        for contract in (
                            positive_contracts
                            if isinstance(positive_contracts, list)
                            else []
                        )
                        if isinstance(contract, Mapping)
                        and _text(contract.get("positive_outcome_contract_id"))
                        is not None
                    }
                )
                if (
                    source_identity is None
                    or not selected_mechanism_ids
                    or not isinstance(replay_inputs, Mapping)
                    or not isinstance(execution, Mapping)
                    or execution.get("argv") != argv
                    or execution.get("command_authorization") != authorization
                    or execution.get("replay_inputs") != replay_inputs
                    or not isinstance(oracle_observation, Mapping)
                    or oracle_observation.get("source_experiment_id") != experiment_id
                    or oracle_observation.get("runner_attested") is not True
                    or oracle_observation.get("replay_observation_sha256")
                    != _canonical_json_sha256(
                        {
                            key: value
                            for key, value in oracle_observation.items()
                            if key != "replay_observation_sha256"
                        }
                    )
                    or oracle_observation.get("positive_outcome_contract_ids")
                    != positive_contract_ids
                    or not positive_contract_ids
                ):
                    continue
                equivalence_proof = content_bound_payload(
                    {
                        "schema_version": 1,
                        "equivalence_mode": "exact_origin_scenario_identity",
                        "source_experiment_id": experiment_id,
                        "origin_atom_ids": [origin_atom_id],
                        "source_identity": source_identity,
                        "source_identity_refs": [
                            "origin_command_identity:"
                            f"{source_identity['source_identity_sha256']}"
                        ],
                        "replay_inputs_sha256": replay_inputs.get(
                            "replay_inputs_sha256"
                        ),
                        "replay_observation_sha256": oracle_observation.get(
                            "replay_observation_sha256"
                        ),
                        "positive_outcome_contract_ids": positive_contract_ids,
                        "selected_mechanism_evidence_ids": selected_mechanism_ids,
                        "outcome_oracle_id": oracle.get("outcome_oracle_id"),
                        "runner_attested": True,
                    },
                    hash_field="equivalence_sha256",
                )
                break
        if faithful and equivalence_proof is None:
            errors.append(f"{label}:faithful_equivalence_unattested")
            continue
        if not requires_live and (not faithful or equivalence_proof is None):
            errors.append(f"{label}:live_waiver_unattested")
            continue
        replay_projection = {
            "experiment_id": experiment_id,
            "executed_argv_sha256": _canonical_json_sha256(argv),
            "command_authorization_sha256": authorization.get("authorization_sha256"),
            "stdout_sha256": replay.get("stdout_sha256"),
            "stderr_sha256": replay.get("stderr_sha256"),
            "replay_inputs_sha256": (
                replay.get("replay_inputs", {}).get("replay_inputs_sha256")
                if isinstance(replay.get("replay_inputs"), Mapping)
                else None
            ),
            "execution_isolation_sha256": _canonical_json_sha256(
                replay.get("execution_isolation")
            ),
        }
        provenance_refs = sorted(
            {
                f"research_experiment:{experiment_id}",
                f"clean_replay:{_canonical_json_sha256(replay_projection)}",
                *selected_mechanism_ids,
                *oracle_ids,
                *proof_ids,
                *(
                    [f"equivalence_proof:{equivalence_proof['equivalence_sha256']}"]
                    if equivalence_proof is not None
                    else []
                ),
            }
        )
        boundary_projection: dict[str, Any] = {
                    "schema_version": 1,
                    "experiment_id": experiment_id,
                    "boundary_kind": boundary_kind,
                    "requires_live_verification": requires_live,
                    "faithful_equivalence": faithful,
                    "provenance_refs": provenance_refs,
                    "rationale_sha256": sha256(rationale.encode("utf-8")).hexdigest(),
                    "runner_attested": True,
        }
        if equivalence_proof is not None:
            boundary_projection["equivalence_proof"] = equivalence_proof
        receipts.append(
            content_bound_payload(
                boundary_projection,
                hash_field="boundary_sha256",
            )
        )
    receipts.sort(key=lambda item: (str(item["experiment_id"]), str(item["boundary_sha256"])))
    return receipts, list(dict.fromkeys(errors))


def _falsification_attempt_receipts(
    dossier: dict[str, Any],
    *,
    clean_replays: dict[str, dict[str, Any]],
    mechanism_evidence: list[dict[str, Any]],
    falsification_interventions: list[dict[str, Any]],
    deterministic_closures: list[dict[str, Any]],
    proof_adapter_receipts: Sequence[Mapping[str, Any]] = (),
    errors: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Bind hypothesis challenges to exact replay and typed-mechanism receipts."""

    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    evidence_by_hypothesis_experiment: dict[tuple[str, str], set[str]] = {}
    symbols_by_hypothesis_experiment: dict[tuple[str, str], set[str]] = {}
    for evidence in mechanism_evidence:
        if not isinstance(evidence, dict):
            continue
        hypothesis_id = _text(evidence.get("hypothesis_id"))
        evidence_id = _text(evidence.get("mechanism_evidence_id"))
        experiment_ids = evidence.get("experiment_ids")
        evidence_symbols = {
            symbol
            for symbol in evidence.get("mechanism_symbols", [])
            if isinstance(symbol, str) and symbol.strip()
        }
        if hypothesis_id is None or evidence_id is None or not isinstance(experiment_ids, list):
            continue
        for experiment_id in experiment_ids:
            if isinstance(experiment_id, str):
                evidence_by_hypothesis_experiment.setdefault(
                    (hypothesis_id, experiment_id), set()
                ).add(evidence_id)
                symbols_by_hypothesis_experiment.setdefault(
                    (hypothesis_id, experiment_id), set()
                ).update(evidence_symbols)
    intervention_by_attempt = {
        (str(receipt.get("hypothesis_id")), str(receipt.get("attempt_id"))): receipt
        for receipt in falsification_interventions
        if isinstance(receipt, dict)
        and _text(receipt.get("hypothesis_id")) is not None
        and _text(receipt.get("attempt_id")) is not None
    }
    proof_by_pair = {
        (
            str(proof.get("hypothesis_id")),
            str(proof.get("intervention", {}).get("baseline_experiment_id")),
            str(proof.get("intervention", {}).get("challenge_experiment_id")),
        ): proof
        for proof in proof_adapter_receipts
        if isinstance(proof, Mapping)
        and isinstance(proof.get("intervention"), Mapping)
        and not validate_causal_proof_receipt(proof)
    }

    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypotheses = hypotheses_raw if isinstance(hypotheses_raw, list) else []
    receipts_by_hypothesis: dict[str, list[dict[str, Any]]] = {}
    deterministic_hypotheses = {
        str(receipt.get("hypothesis_id"))
        for receipt in deterministic_closures
        if isinstance(receipt, dict) and _text(receipt.get("closure_receipt_id")) is not None
    }
    for hypothesis_index, hypothesis in enumerate(hypotheses):
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = _text(hypothesis.get("hypothesis_id")) or f"index-{hypothesis_index}"
        mechanism_raw = hypothesis.get("mechanism_symbols")
        mechanism_symbols = (
            [
                symbol.strip()
                for symbol in mechanism_raw
                if isinstance(symbol, str) and symbol.strip()
            ]
            if isinstance(mechanism_raw, list)
            else []
        )
        support_refs = {
            ref for ref in hypothesis.get("supporting_evidence", []) if isinstance(ref, str)
        }
        counter_refs = {
            ref for ref in hypothesis.get("counterevidence", []) if isinstance(ref, str)
        }
        attempts_raw = hypothesis.get("falsification_attempts")
        attempts = attempts_raw if isinstance(attempts_raw, list) else []
        bound: list[dict[str, Any]] = []
        seen_attempt_ids: set[str] = set()
        for attempt_index, attempt in enumerate(attempts):
            attempt_id = _text(attempt.get("attempt_id")) if isinstance(attempt, dict) else None
            label = attempt_id or f"index-{attempt_index}"
            reasons: list[str] = []
            if not isinstance(attempt, dict):
                reasons.append("not_object")
                attempt = {}
            if attempt_id is None or attempt_id in seen_attempt_ids:
                reasons.append("attempt_id_invalid")
            elif attempt_id is not None:
                seen_attempt_ids.add(attempt_id)
            if attempt.get("hypothesis_id") != hypothesis_id:
                reasons.append("hypothesis_mismatch")
            if attempt.get("claim") != hypothesis.get("statement"):
                reasons.append("claim_mismatch")
            baseline_id = _text(attempt.get("baseline_experiment_id"))
            challenge_id = _text(attempt.get("challenge_experiment_id"))
            outcome = _text(attempt.get("outcome"))
            disproof_condition = attempt.get("disproof_condition")
            intervention_receipt = intervention_by_attempt.get((hypothesis_id, attempt_id or ""))
            proof_receipt = proof_by_pair.get(
                (hypothesis_id, baseline_id or "", challenge_id or "")
            )
            baseline = experiments.get(baseline_id or "")
            challenge = experiments.get(challenge_id or "")
            baseline_replay = clean_replays.get(baseline_id or "")
            challenge_replay = clean_replays.get(challenge_id or "")
            relationship_symbols: list[str] | None = None
            if baseline_id is None or challenge_id is None or baseline_id == challenge_id:
                reasons.append("experiment_identity_invalid")
            if not isinstance(baseline, dict) or not isinstance(challenge, dict):
                reasons.append("experiment_unresolved")
            if not isinstance(baseline_replay, dict) or not isinstance(challenge_replay, dict):
                reasons.append("replay_unresolved")
            if outcome not in _FALSIFICATION_ATTEMPT_OUTCOMES:
                reasons.append("outcome_invalid")
            if not isinstance(disproof_condition, dict):
                reasons.append("disproof_condition_invalid")
            if (
                outcome in {"survived", "disproved"}
                and not isinstance(intervention_receipt, dict)
                and not isinstance(proof_receipt, Mapping)
            ):
                reasons.append("causal_intervention_unverified")

            expected_experiment_outcome = {
                "survived": "supports",
                "disproved": "refutes",
                "inconclusive": "inconclusive",
            }.get(outcome or "")
            if isinstance(baseline, dict) and (
                baseline_id not in support_refs or baseline.get("outcome") != "supports"
            ):
                reasons.append("baseline_not_supporting")
            if isinstance(challenge, dict):
                if challenge.get("outcome") != expected_experiment_outcome:
                    reasons.append("challenge_outcome_mismatch")
                if (
                    not isinstance(proof_receipt, Mapping)
                    and challenge.get("scenario_kind") not in _FALSIFICATION_REPLAY_SCENARIOS
                ):
                    reasons.append("challenge_scenario_invalid")
                if outcome == "survived" and challenge_id not in support_refs:
                    reasons.append("survived_challenge_not_supporting")
                if outcome == "disproved" and challenge_id not in counter_refs:
                    reasons.append("disproved_challenge_not_counterevidence")
            if isinstance(baseline, dict) and isinstance(challenge, dict):
                baseline_atoms = baseline.get("addresses_atom_ids")
                challenge_atoms = challenge.get("addresses_atom_ids")
                if not isinstance(baseline_atoms, list) or not baseline_atoms:
                    reasons.append("baseline_atoms_missing")
                if baseline_atoms != challenge_atoms:
                    reasons.append("source_atoms_mismatch")
                baseline_artifacts = {
                    value
                    for value in (
                        baseline.get("artifact_refs")
                        if isinstance(baseline.get("artifact_refs"), list)
                        else []
                    )
                    if isinstance(value, str)
                }
                challenge_artifacts = {
                    value
                    for value in (
                        challenge.get("artifact_refs")
                        if isinstance(challenge.get("artifact_refs"), list)
                        else []
                    )
                    if isinstance(value, str)
                }
                if (
                    not isinstance(proof_receipt, Mapping)
                    and not baseline_artifacts.intersection(challenge_artifacts)
                ):
                    reasons.append("shared_artifact_missing")
                if (
                    not isinstance(proof_receipt, Mapping)
                    and baseline.get("command") == challenge.get("command")
                ):
                    reasons.append("challenge_reuses_baseline_command")
                if challenge.get("scenario_kind") == "control":
                    relationship = challenge.get("control_relationship")
                    relationship_symbols = (
                        _declared_mechanism_subset(
                            relationship.get("mechanism_symbols"),
                            hypothesis_symbols=mechanism_symbols,
                        )
                        if isinstance(relationship, dict)
                        else None
                    )
                    if (
                        not isinstance(relationship, dict)
                        or relationship.get("supports_experiment_id") != baseline_id
                        or relationship_symbols is None
                    ):
                        reasons.append("control_relationship_unbound")
                observed_assertion = challenge.get("observable_assertion")
                if (
                    not isinstance(disproof_condition, dict)
                    or not isinstance(observed_assertion, dict)
                    or not _falsification_assertion_relation(
                        disproof_condition,
                        observed_assertion,
                        outcome=outcome or "",
                    )
                ):
                    reasons.append("disproof_result_mismatch")

            for role, experiment, replay in (
                ("baseline", baseline, baseline_replay),
                ("challenge", challenge, challenge_replay),
            ):
                if not isinstance(experiment, dict) or not isinstance(replay, dict):
                    continue
                if replay.get("assertion_passed") is not True:
                    reasons.append(f"{role}_assertion_unverified")
                if any(
                    replay.get(receipt_field) != experiment.get(declared_field)
                    for receipt_field, declared_field in (
                        ("command", "command"),
                        ("declared_result", "result"),
                        ("exit_code", "exit_code"),
                        ("outcome", "outcome"),
                        ("scenario_kind", "scenario_kind"),
                        ("observable_assertion", "observable_assertion"),
                    )
                ):
                    reasons.append(f"{role}_receipt_mismatch")
                if not re.fullmatch(r"[0-9a-f]{64}", str(replay.get("stdout_sha256") or "")):
                    reasons.append(f"{role}_stdout_hash_invalid")
                if not re.fullmatch(r"[0-9a-f]{64}", str(replay.get("stderr_sha256") or "")):
                    reasons.append(f"{role}_stderr_hash_invalid")

            baseline_mechanism_ids = evidence_by_hypothesis_experiment.get(
                (hypothesis_id, baseline_id or ""), set()
            )
            challenge_mechanism_ids = evidence_by_hypothesis_experiment.get(
                (hypothesis_id, challenge_id or ""), set()
            )
            baseline_symbols = symbols_by_hypothesis_experiment.get(
                (hypothesis_id, baseline_id or ""), set()
            )
            challenge_symbols = symbols_by_hypothesis_experiment.get(
                (hypothesis_id, challenge_id or ""), set()
            )
            intervention_symbols = (
                _declared_mechanism_subset(
                    intervention_receipt.get("shared_verified_mechanism_symbols"),
                    hypothesis_symbols=mechanism_symbols,
                )
                if isinstance(intervention_receipt, dict)
                else None
            )
            proof_symbols = (
                list(mechanism_symbols)
                if isinstance(proof_receipt, Mapping)
                and proof_receipt.get("hypothesis_id") == hypothesis_id
                else None
            )
            verified_intervention_symbols = intervention_symbols or proof_symbols
            required_relationship_symbols = (
                relationship_symbols or verified_intervention_symbols
            )
            if hypothesis_index == 0:
                if not baseline_mechanism_ids:
                    reasons.append("baseline_mechanism_unbound")
                if not challenge_mechanism_ids:
                    reasons.append("challenge_mechanism_unbound")
                if not required_relationship_symbols:
                    reasons.append("shared_mechanism_subset_missing")
                elif (
                    not set(required_relationship_symbols).issubset(baseline_symbols)
                    or not set(required_relationship_symbols).issubset(challenge_symbols)
                    or verified_intervention_symbols != required_relationship_symbols
                ):
                    reasons.append("shared_mechanism_subset_unverified")
            if reasons:
                errors.append(
                    f"falsification_attempt_unbound:{hypothesis_id}:{label}:"
                    + ",".join(dict.fromkeys(reasons))
                )
                continue
            bound.append(
                {
                    "attempt_id": attempt_id,
                    "hypothesis_id": hypothesis_id,
                    "claim": hypothesis.get("statement"),
                    "baseline_experiment_id": baseline_id,
                    "challenge_experiment_id": challenge_id,
                    "disproof_condition": disproof_condition,
                    "outcome": outcome,
                    "scenario_kind": challenge.get("scenario_kind"),
                    "command": challenge.get("command"),
                    "declared_result": challenge.get("result"),
                    "observable_assertion": challenge.get("observable_assertion"),
                    "exit_code": challenge_replay.get("exit_code"),
                    "stdout_sha256": challenge_replay.get("stdout_sha256"),
                    "stderr_sha256": challenge_replay.get("stderr_sha256"),
                    "mechanism_symbols": required_relationship_symbols,
                    "mechanism_evidence_ids": sorted(challenge_mechanism_ids),
                    "intervention_receipt_id": (
                        intervention_receipt.get("intervention_receipt_id")
                        if isinstance(intervention_receipt, dict)
                        else proof_receipt.get("intervention_id")
                        if isinstance(proof_receipt, Mapping)
                        else None
                    ),
                    "proof_receipt_id": (
                        proof_receipt.get("proof_receipt_id")
                        if isinstance(proof_receipt, Mapping)
                        else None
                    ),
                }
            )
        if (
            hypothesis_index == 0
            and dossier.get("research_status") == "evidence_sufficient"
            and not any(receipt.get("outcome") == "survived" for receipt in bound)
            and hypothesis_id not in deterministic_hypotheses
        ):
            errors.append(f"primary_falsification_survival_missing:{hypothesis_id}")
        receipts_by_hypothesis[hypothesis_id] = sorted(
            bound,
            key=lambda receipt: str(receipt.get("attempt_id")),
        )
    return receipts_by_hypothesis


def _hypothesis_receipts(
    dossier: dict[str, Any],
    *,
    experiment_outcomes: dict[str, str],
    artifact_keys: set[str],
    falsification_attempts: Mapping[str, list[dict[str, Any]]] | None = None,
    errors: list[str],
) -> list[dict[str, Any]]:
    hypotheses = dossier.get("root_cause_hypotheses")
    if not isinstance(hypotheses, list):
        errors.append("root_cause_hypotheses_not_list")
        return []
    receipts: list[dict[str, Any]] = []
    known_refs = set(experiment_outcomes) | artifact_keys
    experiments_raw = dossier.get("experiments")
    experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (experiments_raw if isinstance(experiments_raw, list) else [])
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    for index, hypothesis in enumerate(hypotheses):
        if not isinstance(hypothesis, dict):
            continue
        hypothesis_id = _text(hypothesis.get("hypothesis_id")) or f"index-{index}"
        supporting = hypothesis.get("supporting_evidence")
        counter = hypothesis.get("counterevidence")
        support_refs = (
            [ref.strip() for ref in supporting if isinstance(ref, str)]
            if isinstance(supporting, list)
            else []
        )
        counter_refs = (
            [ref.strip() for ref in counter if isinstance(ref, str)]
            if isinstance(counter, list)
            else []
        )
        unresolved_support = [ref for ref in support_refs if ref not in known_refs]
        unresolved_counter = [ref for ref in counter_refs if ref not in known_refs]
        if unresolved_support:
            errors.append(
                f"hypothesis_support_unresolved:{hypothesis_id}:" + ",".join(unresolved_support)
            )
        if unresolved_counter:
            errors.append(
                f"hypothesis_counterevidence_unresolved:{hypothesis_id}:"
                + ",".join(unresolved_counter)
            )
        control_links: list[dict[str, Any]] = []
        for counter_ref in counter_refs:
            control = experiments.get(counter_ref, {})
            if (
                experiment_outcomes.get(counter_ref) != "refutes"
                or control.get("scenario_kind") != "control"
            ):
                continue
            relationship_raw = control.get("control_relationship")
            relationship = relationship_raw if isinstance(relationship_raw, dict) else {}
            support_id = _text(relationship.get("supports_experiment_id"))
            support = experiments.get(support_id or "", {})
            hypothesis_symbols_raw = hypothesis.get("mechanism_symbols")
            hypothesis_symbols = (
                [
                    symbol.strip()
                    for symbol in hypothesis_symbols_raw
                    if isinstance(symbol, str) and symbol.strip()
                ]
                if isinstance(hypothesis_symbols_raw, list)
                else []
            )
            mechanism_symbols = _declared_mechanism_subset(
                relationship.get("mechanism_symbols"),
                hypothesis_symbols=hypothesis_symbols,
            )
            shared_atom_ids = sorted(
                set(control.get("addresses_atom_ids", []))
                & set(support.get("addresses_atom_ids", []))
            )
            shared_artifact_refs = sorted(
                set(control.get("artifact_refs", [])) & set(support.get("artifact_refs", []))
            )
            valid = (
                support_id in support_refs
                and experiment_outcomes.get(support_id or "") == "supports"
                and mechanism_symbols is not None
                and control.get("addresses_atom_ids") == support.get("addresses_atom_ids")
                and control.get("command") != support.get("command")
                and bool(shared_artifact_refs)
            )
            if not valid:
                errors.append(f"hypothesis_control_unbound:{hypothesis_id}:{counter_ref}")
                continue
            control_links.append(
                {
                    "control_experiment_id": counter_ref,
                    "supports_experiment_id": support_id,
                    "mechanism_symbols": mechanism_symbols,
                    "shared_atom_ids": shared_atom_ids,
                    "shared_artifact_refs": shared_artifact_refs,
                    "controlled_variable": relationship.get("controlled_variable"),
                    "expected_difference": relationship.get("expected_difference"),
                }
            )
        receipts.append(
            {
                "hypothesis_id": hypothesis_id,
                "disposition": hypothesis.get("disposition"),
                "disposition_evidence_refs": hypothesis.get("disposition_evidence", []),
                "supporting_refs": support_refs,
                "counterevidence_refs": counter_refs,
                "mechanism_symbols": hypothesis.get("mechanism_symbols"),
                "control_links": control_links,
                "falsification_attempts": list(
                    (falsification_attempts or {}).get(hypothesis_id, [])
                ),
            }
        )
    return receipts


def _verify_assignment_files(
    assignment: dict[str, Any],
    *,
    expected_atom_ids: list[str],
) -> list[str]:
    errors: list[str] = []
    if assignment.get("assignment_sha256") != evidence_assignment_sha256(assignment):
        errors.append("origin_assignment_hash_mismatch")
    assigned_ids_raw = assignment.get("expected_atom_ids")
    assigned_ids = assigned_ids_raw if isinstance(assigned_ids_raw, list) else []
    if assigned_ids != expected_atom_ids:
        errors.append("origin_assignment_atom_set_mismatch")
    if assignment.get("status") != "complete":
        errors.append("origin_assignment_incomplete")
    receipts_raw = assignment.get("atom_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    receipt_ids: list[str] = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        atom_id = _text(receipt.get("atom_id"))
        if atom_id is not None:
            receipt_ids.append(atom_id)
        atom_snapshot = receipt.get("atom_snapshot")
        if not isinstance(atom_snapshot, dict) or receipt.get(
            "atom_sha256"
        ) != _canonical_json_sha256(atom_snapshot):
            errors.append(f"origin_atom_snapshot_changed:{atom_id}")
        artifacts_raw = receipt.get("artifact_receipts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                errors.append(f"origin_atom_artifact_invalid:{atom_id}")
                continue
            path_raw = _text(artifact.get("path"))
            path = Path(path_raw) if path_raw is not None else None
            if path is None or not path.is_file():
                errors.append(f"origin_atom_artifact_unavailable:{atom_id}:{path_raw}")
                continue
            if (
                artifact.get("sha256") != _sha256_path(path)
                or artifact.get("size_bytes") != path.stat().st_size
            ):
                errors.append(f"origin_atom_artifact_changed:{atom_id}:{path}")
    if receipt_ids != expected_atom_ids:
        errors.append("origin_atom_receipt_coverage_mismatch")
    return errors


def _atom_field_path_value(snapshot: dict[str, Any], field_path: str) -> tuple[bool, Any]:
    """Resolve a restricted immutable ``$.field[index]`` atom snapshot path."""
    if not field_path.startswith("$"):
        return False, None
    current: Any = snapshot
    cursor = 1
    while cursor < len(field_path):
        if field_path[cursor] == ".":
            match = re.match(r"\.([A-Za-z0-9_:-]+)", field_path[cursor:])
            if match is None or not isinstance(current, dict):
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
        return False, None
    return True, current


def _explicit_atom_binding_receipts(
    *,
    experiment: dict[str, Any],
    experiment_id: str,
    atom_id: str,
    atom_receipt: dict[str, Any],
    assertion: dict[str, Any],
    command: str,
    errors: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    declarations_raw = experiment.get("origin_evidence_bindings")
    if not isinstance(declarations_raw, list):
        return [], False
    declarations = [
        declaration
        for declaration in declarations_raw
        if isinstance(declaration, dict) and declaration.get("atom_id") == atom_id
    ]
    if not declarations:
        errors.append(f"experiment_atom_explicit_binding_missing:{experiment_id}:{atom_id}")
        return [], False
    snapshot = atom_receipt.get("atom_snapshot")
    if not isinstance(snapshot, dict):
        errors.append(f"experiment_atom_snapshot_missing:{experiment_id}:{atom_id}")
        return [], False
    verified: list[dict[str, Any]] = []
    direct = False
    for index, declaration in enumerate(declarations):
        role = declaration.get("role")
        field_path = declaration.get("field_path")
        expected_value = declaration.get("value")
        declared_hash = declaration.get("value_sha256")
        prefix = f"experiment_atom_binding_invalid:{experiment_id}:{atom_id}:{index}"
        if role not in {
            "symptom",
            "corroborating",
            "context",
            "command",
            "expected_behavior",
        }:
            errors.append(f"{prefix}:role")
            continue
        if not isinstance(field_path, str) or not field_path.strip():
            errors.append(f"{prefix}:field_path")
            continue
        expected_hash = _canonical_json_sha256(expected_value)
        if declared_hash is not None and declared_hash != expected_hash:
            errors.append(f"{prefix}:value_hash")
            continue
        found, actual_value = _atom_field_path_value(snapshot, field_path)
        if not found or actual_value != expected_value:
            errors.append(f"{prefix}:snapshot_value")
            continue
        binding_is_direct = False
        observation_predicate = declaration.get("observation_predicate")
        generic_predicate_binding = role == "symptom" and isinstance(
            observation_predicate, Mapping
        )
        if observation_predicate is not None and not generic_predicate_binding:
            errors.append(f"{prefix}:observation_predicate_role")
            continue
        if generic_predicate_binding:
            predicate_errors = proof_predicate_contract_errors(observation_predicate)
            predicate_passed, evaluation_errors = evaluate_proof_predicate(
                observation_predicate,
                actual_value,
            )
            if predicate_errors or evaluation_errors or not predicate_passed:
                errors.append(f"{prefix}:observation_predicate_not_bound_to_atom")
                continue
            binding_is_direct = True
        elif role == "command":
            binding_is_direct = (
                field_path == "$.command"
                and isinstance(actual_value, str)
                and _parse_argv_without_shell(actual_value) == _parse_argv_without_shell(command)
            )
        elif role == "symptom":
            source = assertion.get("source")
            operator = assertion.get("operator")
            asserted = assertion.get("expected")
            if source == "exit_code":
                binding_is_direct = (
                    operator == "equals"
                    and field_path.endswith("exit_code")
                    and actual_value == asserted
                    and isinstance(asserted, int)
                    and not isinstance(asserted, bool)
                    and asserted != 0
                )
            elif (
                source in {"stdout", "stderr", "combined"}
                and operator in {"equals", "contains"}
                and isinstance(asserted, str)
                and isinstance(actual_value, str)
            ):
                binding_is_direct = (
                    asserted.strip() == actual_value.strip()
                    if operator == "equals"
                    else asserted in actual_value
                )
        if role in {"symptom", "command"} and not binding_is_direct:
            errors.append(f"{prefix}:not_bound_to_observation")
            continue
        direct = direct or binding_is_direct
        projection: dict[str, Any] = {
                "experiment_id": experiment_id,
                "atom_id": atom_id,
                "match_kind": (
                    "explicit_symptom_field_predicate_declaration"
                    if generic_predicate_binding
                    else f"explicit_{role}_field_binding"
                ),
                "binding_role": role,
                "origin_atom_sha256": atom_receipt.get("atom_sha256"),
                "origin_atom_field_path": field_path,
                "origin_atom_value_sha256": expected_hash,
        }
        if generic_predicate_binding:
            projection.update(
                {
                    "origin_atom_value": actual_value,
                    "observation_predicate": dict(observation_predicate),
                    "observation_predicate_sha256": _canonical_json_sha256(
                        observation_predicate
                    ),
                }
            )
        verified.append(
            content_bound_payload(
                projection,
                hash_field="declared_binding_sha256",
            )
        )
    return verified, direct


def _experiment_atom_bindings(
    dossier: dict[str, Any],
    assignment: dict[str, Any],
    *,
    errors: list[str],
) -> list[dict[str, Any]]:
    receipts_raw = assignment.get("atom_receipts")
    snapshots = {
        str(receipt.get("atom_id")): receipt.get("atom_snapshot")
        for receipt in (receipts_raw if isinstance(receipts_raw, list) else [])
        if isinstance(receipt, dict)
        and _text(receipt.get("atom_id")) is not None
        and isinstance(receipt.get("atom_snapshot"), dict)
    }
    atom_receipts = {
        str(receipt.get("atom_id")): receipt
        for receipt in (receipts_raw if isinstance(receipts_raw, list) else [])
        if isinstance(receipt, dict) and _text(receipt.get("atom_id")) is not None
    }

    def artifact_text_match(atom_id: str, expected: str) -> str | None:
        receipt = atom_receipts.get(atom_id, {})
        artifacts_raw = receipt.get("artifact_receipts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            path_raw = _text(artifact.get("path"))
            path = Path(path_raw) if path_raw is not None else None
            if path is None or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if expected.casefold() in content.casefold():
                return str(path)
        return None

    def snapshot_text_match(value: Any, expected: str, *, path: str = "$") -> str | None:
        if isinstance(value, str):
            return path if expected.casefold() in value.casefold() else None
        if isinstance(value, dict):
            for key, child in value.items():
                match = snapshot_text_match(
                    child,
                    expected,
                    path=f"{path}.{key}",
                )
                if match is not None:
                    return match
        elif isinstance(value, list):
            for index, child in enumerate(value):
                match = snapshot_text_match(
                    child,
                    expected,
                    path=f"{path}[{index}]",
                )
                if match is not None:
                    return match
        return None

    experiments_raw = dossier.get("experiments")
    experiments = experiments_raw if isinstance(experiments_raw, list) else []
    bindings: list[dict[str, Any]] = []
    supported_atoms: set[str] = set()
    directly_supported_atoms: set[str] = set()
    for experiment in experiments:
        if not isinstance(experiment, dict) or experiment.get("outcome") != "supports":
            continue
        scenario_kind = experiment.get("scenario_kind")
        adapter_claim = experiment.get("proof_adapter")
        if scenario_kind == "control" or (
            not isinstance(scenario_kind, str) and not isinstance(adapter_claim, Mapping)
        ):
            continue
        experiment_id = str(experiment.get("experiment_id") or "")
        command = _normalize_command(str(experiment.get("command") or ""))
        assertion_raw = experiment.get("observable_assertion")
        assertion = assertion_raw if isinstance(assertion_raw, dict) else {}
        for atom_id in experiment.get("addresses_atom_ids", []):
            if not isinstance(atom_id, str):
                continue
            snapshot = snapshots.get(atom_id)
            if not isinstance(snapshot, dict):
                errors.append(f"experiment_atom_snapshot_missing:{experiment_id}:{atom_id}")
                continue
            if "origin_evidence_bindings" in experiment:
                explicit, direct = _explicit_atom_binding_receipts(
                    experiment=experiment,
                    experiment_id=experiment_id,
                    atom_id=atom_id,
                    atom_receipt=atom_receipts.get(atom_id, {}),
                    assertion=assertion,
                    command=command,
                    errors=errors,
                )
                if explicit:
                    bindings.extend(explicit)
                    supported_atoms.add(atom_id)
                    if direct:
                        directly_supported_atoms.add(atom_id)
                continue
            match_kind: str | None = None
            atom_command = _text(snapshot.get("command"))
            command_matches = (
                atom_command is not None and _normalize_command(atom_command) == command
            )
            source = assertion.get("source")
            operator = assertion.get("operator")
            expected = assertion.get("expected")
            snapshot_exit_code = snapshot.get("exit_code")
            matched_snapshot_field: str | None = None
            matched_artifact: str | None = None
            if (
                source == "exit_code"
                and operator == "equals"
                and isinstance(expected, int)
                and not isinstance(expected, bool)
                and expected != 0
                and snapshot_exit_code == expected
                and command_matches
            ):
                match_kind = "command_and_exit_code"
            elif source in {"stdout", "stderr", "combined"} and operator in {
                "contains",
                "equals",
            }:
                if (
                    isinstance(expected, str)
                    and len(expected.strip()) >= 12
                    and (
                        command_matches
                        or scenario_kind in {"faithful_replay", "static_trace", "live_runtime"}
                        or isinstance(adapter_claim, Mapping)
                    )
                ):
                    matched_snapshot_field = snapshot_text_match(snapshot, expected)
                    matched_artifact = artifact_text_match(atom_id, expected)
                    if matched_snapshot_field is not None or matched_artifact is not None:
                        match_kind = (
                            "command_and_atom_evidence_symptom"
                            if command_matches and matched_snapshot_field is not None
                            else "faithful_atom_evidence_symptom"
                            if matched_snapshot_field is not None
                            else "command_and_artifact_symptom_text"
                            if command_matches
                            else "faithful_artifact_symptom_text"
                        )
            if match_kind is None:
                errors.append(f"experiment_not_bound_to_atom:{experiment_id}:{atom_id}")
                continue
            supported_atoms.add(atom_id)
            directly_supported_atoms.add(atom_id)
            binding = {
                "experiment_id": experiment_id,
                "atom_id": atom_id,
                "match_kind": match_kind,
            }
            if matched_snapshot_field is not None:
                binding["origin_atom_field_path"] = matched_snapshot_field
                found, matched_value = _atom_field_path_value(
                    snapshot,
                    matched_snapshot_field,
                )
                if found:
                    binding["origin_atom_value_sha256"] = _canonical_json_sha256(matched_value)
            binding["origin_atom_sha256"] = atom_receipts.get(atom_id, {}).get("atom_sha256")
            if (
                match_kind
                in {
                    "command_and_artifact_symptom_text",
                    "faithful_artifact_symptom_text",
                }
                or matched_artifact is not None
            ):
                binding["origin_artifact_path"] = matched_artifact
                if matched_artifact is not None:
                    binding["origin_artifact_sha256"] = _sha256_path(Path(matched_artifact))
            bindings.append(binding)
    expected_raw = assignment.get("expected_atom_ids")
    expected = {
        atom_id
        for atom_id in (expected_raw if isinstance(expected_raw, list) else [])
        if isinstance(atom_id, str)
    }
    requires_advancing_coverage = dossier.get("research_status") not in {
        "insufficient_evidence",
        "blocked",
    }
    if (
        requires_advancing_coverage
        and assignment.get("status") == "complete"
        and supported_atoms != expected
    ):
        errors.append("supporting_experiments_do_not_cover_origin_atoms")
    if (
        requires_advancing_coverage
        and assignment.get("status") == "complete"
        and expected
        and not directly_supported_atoms
    ):
        errors.append("supporting_experiments_have_no_direct_symptom_binding")
    return bindings


def _implementation_touchpoint_receipts(
    claim: Mapping[str, Any],
    *,
    proof_receipt: Mapping[str, Any],
    inspected_file_receipts: Sequence[Mapping[str, Any]],
    inspected_symbol_receipts: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind optional model-described change surfaces to runner-observed repository reads.

    A causal locator such as ``env:MODE`` or ``fs:state.json`` explains the intervention, but it
    is not a repository path.  The model may name a connected production touchpoint; the runner
    projects it only when the exact file (and every supplied symbol) was actually inspected.
    Invalid ancillary touchpoints are omitted without discarding an otherwise valid causal proof.
    """

    raw_touchpoints = claim.get("implementation_touchpoints")
    if raw_touchpoints is None:
        return [], []
    if not isinstance(raw_touchpoints, list) or not raw_touchpoints or len(raw_touchpoints) > 32:
        return [], ["proof_adapter_implementation_touchpoints_invalid"]

    intervention = proof_receipt.get("intervention")
    causal_target = (
        _text(intervention.get("target")) if isinstance(intervention, Mapping) else None
    )
    if causal_target is None:
        return [], ["proof_adapter_implementation_touchpoint_causal_target_unavailable"]

    files_by_path = {
        str(receipt["path"]).replace("\\", "/").removeprefix("./"): receipt
        for receipt in inspected_file_receipts
        if isinstance(receipt, Mapping) and _text(receipt.get("path")) is not None
    }
    inspected_symbols = {
        (
            str(receipt["path"]).replace("\\", "/").removeprefix("./"),
            str(receipt["symbol"]),
        )
        for receipt in inspected_symbol_receipts
        if isinstance(receipt, Mapping)
        and _text(receipt.get("path")) is not None
        and _text(receipt.get("symbol")) is not None
    }
    receipts: dict[str, dict[str, Any]] = {}
    diagnostics: list[str] = []
    for index, raw in enumerate(raw_touchpoints):
        label = f"proof_adapter_implementation_touchpoint_invalid:{index}"
        if not isinstance(raw, Mapping):
            diagnostics.append(label)
            continue
        path_raw = _text(raw.get("path"))
        path = (
            path_raw.replace("\\", "/").removeprefix("./")
            if path_raw is not None
            else None
        )
        causal_locator = _text(raw.get("causal_locator")) or causal_target
        symbols_raw = raw.get("symbols", [])
        relationship = _text(raw.get("relationship"))
        if (
            path is None
            or path.startswith(".usertest_research/")
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or causal_locator != causal_target
            or not isinstance(symbols_raw, list)
            or any(_text(symbol) is None for symbol in symbols_raw)
            or relationship is None
        ):
            diagnostics.append(label)
            continue
        symbols = sorted({str(symbol).strip() for symbol in symbols_raw})
        file_receipt = files_by_path.get(path)
        if file_receipt is None or any(
            (path, symbol) not in inspected_symbols for symbol in symbols
        ):
            diagnostics.append(label)
            continue
        observed_sha = _text(file_receipt.get("observed_content_sha256"))
        if observed_sha is None or re.fullmatch(r"[0-9a-f]{64}", observed_sha) is None:
            observed_sha = (
                _text(file_receipt.get("sha256"))
                if file_receipt.get("whole_file_observed") is True
                else None
            )
        if observed_sha is None or re.fullmatch(r"[0-9a-f]{64}", observed_sha) is None:
            diagnostics.append(label)
            continue
        projection = {
            "causal_locator": causal_locator,
            "path": path,
            "symbols": symbols,
            "relationship": relationship,
            "runner_attested": True,
            "inspected_content_sha256": observed_sha,
        }
        evidence_sha256 = _canonical_json_sha256(projection)
        receipt = {
            "touchpoint_id": f"implementation_touchpoint:{evidence_sha256}",
            **projection,
            "evidence_sha256": evidence_sha256,
        }
        receipts[receipt["touchpoint_id"]] = receipt
    return [receipts[key] for key in sorted(receipts)], list(dict.fromkeys(diagnostics))


def _proof_adapter_receipts(
    dossier: Mapping[str, Any],
    *,
    case_id: str,
    problem_id: str,
    experiments: Mapping[str, Mapping[str, Any]],
    clean_replays: Mapping[str, Mapping[str, Any]],
    evidence_assignment: Mapping[str, Any],
    atom_bindings: Sequence[Mapping[str, Any]],
    planning_workspace: Path | None,
    symbol_receipts: Sequence[Mapping[str, Any]],
    artifact_receipts: Sequence[Mapping[str, Any]],
    inspected_file_receipts: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    registry = builtin_proof_adapter_registry()
    basis_registry = builtin_positive_basis_registry()
    hypotheses_raw = dossier.get("root_cause_hypotheses")
    hypothesis_ids = {
        str(item.get("hypothesis_id"))
        for item in hypotheses_raw if isinstance(item, Mapping)
    } if isinstance(hypotheses_raw, list) else set()
    receipts: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for experiment_id, experiment in experiments.items():
        claim = experiment.get("proof_adapter")
        if not isinstance(claim, Mapping):
            continue
        unit_errors: list[str] = []
        hypothesis_id = _text(claim.get("hypothesis_id"))
        baseline_id = _text(claim.get("baseline_experiment_id"))
        challenge_id = _text(claim.get("challenge_experiment_id"))
        if (
            hypothesis_id is None
            or hypothesis_id not in hypothesis_ids
            or baseline_id is None
            or challenge_id is None
        ):
            unit_errors.append("proof_adapter_unresolved_identity")
            diagnostics.append(
                {
                    "experiment_id": experiment_id,
                    "adapter_id": claim.get("adapter_id"),
                    "claim_sha256": _canonical_json_sha256(claim),
                    "diagnostics": unit_errors,
                }
            )
            continue
        pair_ids = {baseline_id, challenge_id}
        pair_bindings = [
            dict(binding)
            for binding in atom_bindings
            if isinstance(binding, Mapping)
            and binding.get("experiment_id") in pair_ids
            and _text(binding.get("atom_id")) is not None
        ]
        predicate_candidates = [
            binding
            for binding in pair_bindings
            if binding.get("binding_role") == "symptom"
            and isinstance(binding.get("observation_predicate"), Mapping)
        ]
        legacy_source_atom_ids = {
            str(binding["atom_id"])
            for binding in pair_bindings
            if (
                binding.get("binding_role") == "symptom"
                and not isinstance(binding.get("observation_predicate"), Mapping)
            )
            or (
                binding.get("binding_role") is None
                and _text(binding.get("match_kind")) is not None
            )
        }
        provisional_source_atom_ids = legacy_source_atom_ids | {
            str(binding["atom_id"]) for binding in predicate_candidates
        }
        if not provisional_source_atom_ids:
            unit_errors.append("proof_adapter_source_root_unbound")
            diagnostics.append(
                {
                    "experiment_id": experiment_id,
                    "adapter_id": claim.get("adapter_id"),
                    "claim_sha256": _canonical_json_sha256(claim),
                    "diagnostics": unit_errors,
                }
            )
            continue
        positive = claim.get("positive_outcome")
        semantic = positive.get("semantic_basis") if isinstance(positive, Mapping) else None
        predicate = positive.get("predicate") if isinstance(positive, Mapping) else None

        def evaluate_adapter(
            source_atom_ids: set[str],
            *,
            attested_predicate_bindings: Sequence[Mapping[str, Any]] = (),
            semantic_claim: Mapping[str, Any] = (
                semantic if isinstance(semantic, Mapping) else {}
            ),
            positive_predicate: Mapping[str, Any] = (
                predicate if isinstance(predicate, Mapping) else {}
            ),
            source_pair_ids: frozenset[str] = frozenset(pair_ids),
            source_pair_bindings: tuple[dict[str, Any], ...] = tuple(pair_bindings),
            resolved_hypothesis_id: str = hypothesis_id,
            adapter_claim: Mapping[str, Any] = claim,
        ) -> tuple[Any, Any]:
            basis = basis_registry.evaluate(
                PositiveBasisContext(
                    semantic_claim=semantic_claim,
                    predicate=positive_predicate,
                    source_atom_ids=frozenset(source_atom_ids),
                    evidence_assignment=evidence_assignment,
                    experiments=experiments,
                    clean_replays=clean_replays,
                    planning_workspace=planning_workspace,
                    artifact_receipts=artifact_receipts,
                    symbol_receipts=symbol_receipts,
                )
            )
            if basis.basis is None:
                return basis, None
            source_projection: dict[str, Any] = {
                "root_kind": "origin_symptom",
                "origin_atom_ids": sorted(source_atom_ids),
                "source_experiment_ids": sorted(source_pair_ids),
                "symptom_bindings_sha256": _canonical_json_sha256(source_pair_bindings),
                "positive_basis": basis.basis,
                "runner_attested": True,
            }
            if attested_predicate_bindings:
                source_projection["atom_field_predicate_bindings"] = sorted(
                    (dict(binding) for binding in attested_predicate_bindings),
                    key=lambda binding: (
                        str(binding.get("atom_id")),
                        str(binding.get("origin_atom_field_path")),
                    ),
                )
            source_root = content_bound_payload(
                source_projection,
                hash_field="source_root_sha256",
            )
            adapter_result = registry.evaluate(
                ProofAdapterContext(
                    case_id=case_id,
                    problem_id=problem_id,
                    hypothesis_id=resolved_hypothesis_id,
                    claim=adapter_claim,
                    experiments=experiments,
                    clean_replays=clean_replays,
                    source_root=source_root,
                    planning_workspace=planning_workspace,
                    atom_bindings=atom_bindings,
                    symbol_receipts=symbol_receipts,
                    artifact_receipts=artifact_receipts,
                    services={},
                )
            )
            return basis, adapter_result

        basis_result, result = evaluate_adapter(provisional_source_atom_ids)
        unit_errors.extend(basis_result.diagnostics)
        if basis_result.basis is None or result is None:
            diagnostics.append(
                {
                    "experiment_id": experiment_id,
                    "adapter_id": claim.get("adapter_id"),
                    "claim_sha256": _canonical_json_sha256(claim),
                    "diagnostics": list(dict.fromkeys(unit_errors)),
                }
            )
            continue
        unit_errors.extend(result.diagnostics)

        if predicate_candidates:
            provisional_proof = result.receipts[0] if result.receipts else None
            observations = (
                provisional_proof.get("observations")
                if isinstance(provisional_proof, Mapping)
                else None
            )
            baseline_observation = (
                observations.get("baseline") if isinstance(observations, Mapping) else None
            )
            attested_predicate_bindings: list[dict[str, Any]] = []
            for binding in predicate_candidates:
                declared_projection = {
                    key: value
                    for key, value in binding.items()
                    if key != "declared_binding_sha256"
                }
                predicate_contract = binding.get("observation_predicate")
                baseline_value = (
                    baseline_observation.get("observed")
                    if isinstance(baseline_observation, Mapping)
                    else None
                )
                predicate_passed, predicate_errors = evaluate_proof_predicate(
                    predicate_contract,
                    baseline_value,
                )
                if (
                    binding.get("experiment_id") != baseline_id
                    or binding.get("declared_binding_sha256")
                    != _canonical_json_sha256(declared_projection)
                    or not isinstance(baseline_observation, Mapping)
                    or predicate_errors
                    or not predicate_passed
                ):
                    unit_errors.append(
                        "proof_adapter_atom_predicate_not_bound_to_baseline:"
                        f"{binding.get('atom_id')}"
                    )
                    continue
                attested_predicate_bindings.append(
                    content_bound_payload(
                        {
                            "atom_id": binding.get("atom_id"),
                            "origin_atom_sha256": binding.get("origin_atom_sha256"),
                            "origin_atom_field_path": binding.get(
                                "origin_atom_field_path"
                            ),
                            "origin_atom_value": binding.get("origin_atom_value"),
                            "origin_atom_value_sha256": binding.get(
                                "origin_atom_value_sha256"
                            ),
                            "observation_predicate": dict(predicate_contract),
                            "observation_predicate_sha256": binding.get(
                                "observation_predicate_sha256"
                            ),
                            "baseline_experiment_id": baseline_id,
                            "baseline_observation_sha256": baseline_observation.get(
                                "observation_sha256"
                            ),
                            "adapter_id": provisional_proof.get("adapter_id"),
                            "adapter_version": provisional_proof.get("adapter_version"),
                            "runner_attested": True,
                        },
                        hash_field="atom_field_binding_sha256",
                    )
                )
            final_source_atom_ids = legacy_source_atom_ids | {
                str(binding["atom_id"]) for binding in attested_predicate_bindings
            }
            if not final_source_atom_ids:
                result = None
                unit_errors.append("proof_adapter_source_root_unbound")
            else:
                final_basis, final_result = evaluate_adapter(
                    final_source_atom_ids,
                    attested_predicate_bindings=attested_predicate_bindings,
                )
                unit_errors.extend(final_basis.diagnostics)
                result = final_result
                if result is not None:
                    unit_errors.extend(result.diagnostics)
        if result is None:
            diagnostics.append(
                {
                    "experiment_id": experiment_id,
                    "adapter_id": claim.get("adapter_id"),
                    "claim_sha256": _canonical_json_sha256(claim),
                    "diagnostics": list(dict.fromkeys(unit_errors)),
                }
            )
            continue
        bound_receipts: list[dict[str, Any]] = []
        for proof_receipt in result.receipts:
            touchpoints, touchpoint_diagnostics = _implementation_touchpoint_receipts(
                claim,
                proof_receipt=proof_receipt,
                inspected_file_receipts=inspected_file_receipts,
                inspected_symbol_receipts=symbol_receipts,
            )
            unit_errors.extend(touchpoint_diagnostics)
            if not touchpoints:
                bound_receipts.append(proof_receipt)
                continue
            bound = dict(proof_receipt)
            adapter_evidence_raw = bound.get("adapter_evidence")
            adapter_evidence = (
                dict(adapter_evidence_raw)
                if isinstance(adapter_evidence_raw, Mapping)
                else {}
            )
            adapter_evidence["implementation_touchpoints"] = touchpoints
            bound["adapter_evidence"] = adapter_evidence
            bound["proof_receipt_id"] = proof_receipt_id_for(bound)
            bound_errors = validate_causal_proof_receipt(bound)
            if bound_errors:
                unit_errors.extend(
                    f"proof_adapter_implementation_touchpoint_receipt_invalid:{error}"
                    for error in bound_errors
                )
                bound_receipts.append(proof_receipt)
            else:
                bound_receipts.append(bound)
        if unit_errors:
            diagnostics.append(
                {
                    "experiment_id": experiment_id,
                    "adapter_id": claim.get("adapter_id"),
                    "claim_sha256": _canonical_json_sha256(claim),
                    "diagnostics": list(dict.fromkeys(unit_errors)),
                }
            )
        receipts.extend(bound_receipts)
    by_id = {str(receipt["proof_receipt_id"]): receipt for receipt in receipts}
    return [by_id[key] for key in sorted(by_id)], diagnostics


def verify_research_evidence(
    dossier: dict[str, Any],
    *,
    run_dir: Path,
    repo_revision: str | None,
    case_id: str,
    problem_id: str,
    expected_case_id: str | None,
    expected_problem_id: str | None,
    evidence_assignment: dict[str, Any],
    evidence_atom_ids: list[str],
    revision_view_destination: Path,
    replay_timeout_seconds: float | None,
    requested_repo_ref: str | None,
    resolved_repo_ref: str | None,
    replay_executor: ReplayExecutor | None = None,
) -> dict[str, Any]:
    """Return a runner-owned receipt binding dossier claims to retained evidence."""
    errors: list[str] = []
    run_dir = run_dir.resolve()
    workspace = _read_workspace(run_dir)
    if workspace is None or not workspace.is_dir():
        errors.append("workspace_unavailable")
        workspace = None

    head = _workspace_head(workspace) if workspace is not None else None
    if repo_revision is None:
        errors.append("repo_revision_unavailable")
    target_ref_path = run_dir / "target_ref.json"
    target_ref: dict[str, Any] = {}
    if target_ref_path.is_file():
        try:
            target_ref_raw = json.loads(target_ref_path.read_text(encoding="utf-8"))
            target_ref = target_ref_raw if isinstance(target_ref_raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            errors.append("target_ref_unreadable")
    else:
        errors.append("target_ref_missing")
    if target_ref.get("ref") != resolved_repo_ref:
        errors.append("target_ref_acquisition_ref_mismatch")
    if target_ref.get("commit_sha") != repo_revision:
        errors.append("target_ref_commit_mismatch")
    if head is None:
        errors.append("workspace_revision_unverifiable")
    elif repo_revision is not None and head.casefold() != repo_revision.casefold():
        errors.append(f"workspace_revision_mismatch:{head}:{repo_revision}")

    if expected_problem_id != problem_id:
        errors.append(
            f"problem_id_attestation_mismatch:{expected_problem_id or 'missing'}:{problem_id}"
        )
    if expected_case_id != case_id:
        errors.append(f"case_id_attestation_mismatch:{expected_case_id or 'missing'}:{case_id}")
    errors.extend(
        _verify_assignment_files(
            evidence_assignment,
            expected_atom_ids=evidence_atom_ids,
        )
    )
    atom_bindings = _experiment_atom_bindings(
        dossier,
        evidence_assignment,
        errors=errors,
    )

    planning_workspace: Path | None = None
    planning_head: str | None = None
    planning_clean: bool | None = None
    if workspace is not None and repo_revision is not None:
        planning_workspace, planning_head, planning_clean, planning_errors = (
            materialize_clean_revision_view(
                source_workspace=workspace,
                destination=revision_view_destination,
                repo_revision=repo_revision,
            )
        )
        errors.extend(planning_errors)

    workspace_overlay: dict[str, Any] = {}
    if workspace is not None and planning_workspace is not None:
        overlay_errors, workspace_overlay = _workspace_overlay_errors(
            research_workspace=workspace,
            baseline_workspace=planning_workspace,
        )
        errors.extend(overlay_errors)
        has_research_writes = bool(
            workspace_overlay.get("changed_baseline_paths")
            or workspace_overlay.get("research_overlay_paths")
            or workspace_overlay.get("suspicious_extra_paths")
        )
        if dossier.get("writes_used") is not has_research_writes:
            errors.append("writes_used_mismatch_complete_workspace_diff")
        dossier["diff_classification"] = _verified_diff_classification(
            dossier.get("diff_classification"),
            workspace_overlay,
        )

    events_path = run_dir / "normalized_events.jsonl"
    events = _load_events(events_path, errors)
    clean_replays: dict[str, dict[str, Any]] = {}
    executor: ReplayExecutor = replay_executor or BlockedReplayExecutor()
    replay_isolation = executor.isolation_receipt(
        source_workspace=workspace if workspace is not None else run_dir
    )
    errors.extend(_isolation_receipt_errors(replay_isolation))
    if planning_workspace is not None and repo_revision is not None:
        clean_replays = _clean_replay_receipts(
            dossier,
            evidence_assignment=evidence_assignment,
            baseline_workspace=planning_workspace,
            research_workspace=workspace if workspace is not None else planning_workspace,
            overlay_manifest=(
                workspace_overlay.get("research_overlay_manifest", {})
                if isinstance(workspace_overlay, dict)
                else {}
            ),
            replay_root=run_dir / "evidence_replays",
            repo_revision=repo_revision,
            timeout_seconds=replay_timeout_seconds,
            errors=errors,
            replay_executor=executor,
        )
    artifact_receipts, artifact_keys = _artifact_receipts(
        dossier,
        run_dir=run_dir,
        workspace=workspace,
        errors=errors,
    )
    experiment_receipts, experiment_outcomes = _experiment_receipts(
        dossier,
        events=events,
        artifact_keys=artifact_keys,
        clean_replays=clean_replays,
        errors=errors,
    )
    file_receipts, symbol_receipts = _inspection_receipts(
        dossier,
        workspace=planning_workspace,
        events=events,
        errors=errors,
    )
    declared_experiments_raw = dossier.get("experiments")
    declared_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            declared_experiments_raw if isinstance(declared_experiments_raw, list) else []
        )
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    proof_adapter_receipts, proof_adapter_diagnostics = _proof_adapter_receipts(
        dossier,
        case_id=case_id,
        problem_id=problem_id,
        experiments=declared_experiments,
        clean_replays=clean_replays,
        evidence_assignment=evidence_assignment,
        atom_bindings=atom_bindings,
        planning_workspace=planning_workspace,
        symbol_receipts=symbol_receipts,
        artifact_receipts=artifact_receipts,
        inspected_file_receipts=file_receipts,
    )
    # Exception traces and exact pytest AST controls are strong optional proof
    # modes.  Their absence is expected for non-throwing and runtime/config
    # failures, so collect their receipts without making their shape universal.
    optional_trace_errors: list[str] = []
    causal_links = _causal_link_receipts(
        dossier,
        clean_replays=clean_replays,
        symbol_receipts=symbol_receipts,
        errors=optional_trace_errors,
    )
    test_selections: list[dict[str, Any]] = []
    control_verifications: list[dict[str, Any]] = []
    failure_paths: list[dict[str, Any]] = []
    falsification_interventions: list[dict[str, Any]] = []
    deterministic_closures: list[dict[str, Any]] = []
    optional_control_errors: list[str] = []
    optional_falsification_intervention_errors: list[str] = []
    if planning_workspace is not None:
        test_selections, control_verifications = _causal_control_receipts(
            dossier,
            clean_replays=clean_replays,
            planning_workspace=planning_workspace,
            symbol_receipts=symbol_receipts,
            errors=optional_control_errors,
        )
        if optional_control_errors:
            test_selections = []
            control_verifications = []
        failure_paths = _failure_path_receipts(
            dossier,
            test_selections=test_selections,
            control_verifications=control_verifications,
            errors=errors,
        )
        falsification_interventions = _falsification_intervention_receipts(
            dossier,
            clean_replays=clean_replays,
            planning_workspace=planning_workspace,
            symbol_receipts=symbol_receipts,
            errors=optional_falsification_intervention_errors,
        )
        if optional_falsification_intervention_errors and not proof_adapter_receipts:
            errors.extend(optional_falsification_intervention_errors)
    preliminary_mechanism_errors: list[str] = []
    preliminary_mechanism_evidence = _typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=clean_replays,
        symbol_receipts=symbol_receipts,
        causal_links=causal_links,
        strong_controls=control_verifications,
        falsification_interventions=falsification_interventions,
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=preliminary_mechanism_errors,
    )
    deterministic_closures = _deterministic_mechanism_closure_receipts(
        dossier,
        clean_replays=clean_replays,
        symbol_receipts=symbol_receipts,
        mechanism_evidence=preliminary_mechanism_evidence,
    )
    mechanism_evidence = _typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=clean_replays,
        symbol_receipts=symbol_receipts,
        causal_links=causal_links,
        strong_controls=control_verifications,
        falsification_interventions=falsification_interventions,
        deterministic_closures=deterministic_closures,
        proof_adapter_receipts=proof_adapter_receipts,
        atom_bindings=atom_bindings,
        errors=errors,
    )
    (
        verified_mechanism,
        verified_mechanism_sha256,
        verified_mechanism_provenance,
        verified_mechanism_provenance_sha256,
    ) = _verified_mechanism_projection(
        dossier,
        mechanism_evidence=mechanism_evidence,
        control_verifications=control_verifications,
        falsification_interventions=falsification_interventions,
        deterministic_closures=deterministic_closures,
    )
    outcome_oracles = _outcome_oracle_receipts(
        dossier,
        clean_replays=clean_replays,
        mechanism_evidence=mechanism_evidence,
        proof_adapter_receipts=proof_adapter_receipts,
        verified_mechanism=verified_mechanism,
        verified_mechanism_sha256=verified_mechanism_sha256,
        verified_mechanism_provenance=verified_mechanism_provenance,
        verified_mechanism_provenance_sha256=verified_mechanism_provenance_sha256,
        control_verifications=control_verifications,
        falsification_interventions=falsification_interventions,
        inspected_file_receipts=file_receipts,
        inspected_symbol_receipts=symbol_receipts,
        evidence_assignment=evidence_assignment,
        atom_bindings=atom_bindings,
        planning_workspace=planning_workspace,
        research_workspace=workspace,
        overlay_manifest=(
            workspace_overlay.get("research_overlay_manifest", {})
            if isinstance(workspace_overlay, dict)
            else {}
        ),
        run_dir=run_dir,
        repo_revision=repo_revision,
        errors=errors,
    )
    verification_boundaries, verification_boundary_errors = (
        _verification_boundary_receipts(
            experiments=declared_experiments,
            clean_replays=clean_replays,
            mechanism_evidence=mechanism_evidence,
            proof_adapter_receipts=proof_adapter_receipts,
            outcome_oracles=outcome_oracles,
            verified_mechanism_provenance=verified_mechanism_provenance,
        )
    )
    errors.extend(verification_boundary_errors)
    falsification_attempts = _falsification_attempt_receipts(
        dossier,
        clean_replays=clean_replays,
        mechanism_evidence=mechanism_evidence,
        falsification_interventions=falsification_interventions,
        deterministic_closures=deterministic_closures,
        proof_adapter_receipts=proof_adapter_receipts,
        errors=errors,
    )
    hypothesis_receipts = _hypothesis_receipts(
        dossier,
        experiment_outcomes=experiment_outcomes,
        artifact_keys=artifact_keys,
        falsification_attempts=falsification_attempts,
        errors=errors,
    )

    report_path = run_dir / "report.json"
    unique_errors = list(dict.fromkeys(errors))
    quarantined_diagnostics = [
        {
            "component": component,
            "diagnostics": list(dict.fromkeys(component_errors)),
        }
        for component, component_errors in (
            ("optional_causal_trace", optional_trace_errors),
            ("optional_control", optional_control_errors),
            (
                "optional_falsification_intervention",
                optional_falsification_intervention_errors
                if proof_adapter_receipts
                else [],
            ),
            ("preliminary_mechanism_projection", preliminary_mechanism_errors),
        )
        if component_errors
    ]
    receipt = {
        "verification_method": _VERIFICATION_METHOD,
        "status": "verified" if not unique_errors else "failed",
        "case_id": case_id,
        "problem_id": problem_id,
        "repo_revision": repo_revision,
        "requested_repo_ref": requested_repo_ref,
        "resolved_repo_ref": resolved_repo_ref,
        "workspace_dir": str(workspace) if workspace is not None else None,
        "workspace_head": head,
        "workspace_overlay": workspace_overlay,
        "replay_isolation": replay_isolation,
        "planning_workspace_dir": (
            str(planning_workspace) if planning_workspace is not None else None
        ),
        "planning_workspace_head": planning_head,
        "planning_workspace_clean": planning_clean,
        "run_dir": str(run_dir),
        "origin_atom_ids": list(dict.fromkeys(evidence_atom_ids)),
        "assignment_sha256": evidence_assignment.get("assignment_sha256"),
        "claims_sha256": research_claims_sha256(dossier),
        "normalized_events_sha256": (
            _sha256_path(events_path) if events_path.exists() and events_path.is_file() else None
        ),
        "run_report_sha256": (
            _sha256_path(report_path) if report_path.exists() and report_path.is_file() else None
        ),
        "artifacts": artifact_receipts,
        "experiments": experiment_receipts,
        "inspected_files": file_receipts,
        "inspected_symbols": symbol_receipts,
        "hypothesis_refs": hypothesis_receipts,
        "causal_links": causal_links,
        "mechanism_evidence": mechanism_evidence,
        "proof_adapter_receipts": proof_adapter_receipts,
        "proof_adapter_diagnostics": proof_adapter_diagnostics,
        "quarantined_diagnostics": quarantined_diagnostics,
        "verified_mechanism": verified_mechanism,
        "verified_mechanism_sha256": verified_mechanism_sha256,
        "verified_mechanism_provenance": verified_mechanism_provenance,
        "verified_mechanism_provenance_sha256": (verified_mechanism_provenance_sha256),
        "outcome_oracles": outcome_oracles,
        "verification_boundaries": verification_boundaries,
        "test_selections": test_selections,
        "control_verifications": control_verifications,
        "falsification_interventions": falsification_interventions,
        "deterministic_mechanism_closures": deterministic_closures,
        "failure_paths": failure_paths,
        "atom_bindings": atom_bindings,
        "errors": unique_errors,
    }
    receipt["receipt_sha256"] = evidence_verification_sha256(receipt)
    return receipt


def _persisted_origin_attachment_errors(
    *,
    assignment: dict[str, Any],
    receipt: dict[str, Any],
    research_workspace: Path | None,
    persisted_events: Sequence[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    assignment_manifest_raw = assignment.get("origin_attachment_evidence")
    assignment_manifest = (
        assignment_manifest_raw if isinstance(assignment_manifest_raw, dict) else {}
    )
    receipt_manifest_raw = receipt.get("origin_attachment_evidence")
    receipt_manifest = receipt_manifest_raw if isinstance(receipt_manifest_raw, dict) else {}
    if not assignment_manifest and not receipt_manifest:
        return []
    if receipt_manifest != assignment_manifest:
        errors.append("research_origin_attachment_manifest_changed")
    if research_workspace is None or not research_workspace.is_dir():
        return [*errors, "research_origin_attachment_workspace_unavailable"]
    errors.extend(
        verify_materialized_origin_attachments(
            workspace_dir=research_workspace,
            manifest=assignment_manifest,
        )
    )
    materialization_errors_raw = assignment_manifest.get("errors")
    if isinstance(materialization_errors_raw, list) and materialization_errors_raw:
        errors.append("research_origin_attachment_materialization_not_complete")

    expected = {
        str(requirement["file"]): requirement
        for requirement in origin_attachment_requirements(assignment_manifest)
    }
    attestations_raw = receipt.get("origin_attachment_read_attestations")
    attestations = attestations_raw if isinstance(attestations_raw, list) else []
    observed: set[str] = set()
    for attestation in attestations:
        if not isinstance(attestation, dict):
            errors.append("research_origin_attachment_read_attestation_invalid")
            continue
        rel_path = _text(attestation.get("file"))
        requirement = expected.get(rel_path or "")
        event_index = attestation.get("read_event_index")
        if (
            rel_path is None
            or requirement is None
            or isinstance(event_index, bool)
            or not isinstance(event_index, int)
            or event_index < 0
            or event_index >= len(persisted_events)
        ):
            errors.append("research_origin_attachment_read_attestation_fields_invalid")
            continue
        path = (research_workspace / Path(rel_path)).resolve()
        try:
            path.relative_to(research_workspace.resolve())
        except ValueError:
            errors.append(f"research_origin_attachment_read_outside_workspace:{rel_path}")
            continue
        event = persisted_events[event_index]
        data_raw = event.get("data")
        data = data_raw if isinstance(data_raw, dict) else {}
        event_path = _text(data.get("path"))
        normalized_event_path = (event_path or "").replace("\\", "/").casefold()
        normalized_rel = rel_path.replace("\\", "/").casefold()
        if (
            not path.is_file()
            or _sha256_path(path) != requirement.get("sha256")
            or path.stat().st_size != requirement.get("size_bytes")
            or attestation.get("file_sha256") != requirement.get("sha256")
            or attestation.get("file_size_bytes") != requirement.get("size_bytes")
            or event.get("type") != "read_file"
            or not (
                normalized_event_path == normalized_rel
                or normalized_event_path.endswith("/" + normalized_rel)
            )
            or data.get("content_observed") is not True
            or data.get("whole_file_observed") is not True
            or data.get("source_exit_code") != 0
            or data.get("file_sha256") != requirement.get("sha256")
            or data.get("file_size_bytes") != requirement.get("size_bytes")
            or attestation.get("read_event_sha256") != _canonical_json_sha256(event)
        ):
            errors.append(f"research_origin_attachment_read_attestation_changed:{rel_path}")
            continue
        if rel_path in observed:
            errors.append(f"research_origin_attachment_read_attestation_duplicate:{rel_path}")
            continue
        observed.add(rel_path)
    if observed != set(expected):
        errors.append("research_origin_attachment_read_coverage_mismatch")
    return errors


def _persisted_research_attempt_errors(dossier: dict[str, Any]) -> list[str]:
    """Rehash retained attempts and reverify each Codex subscription/session contract."""
    attempts_raw = dossier.get("research_attempts")
    attempts = attempts_raw if isinstance(attempts_raw, list) else []
    errors: list[str] = []
    for attempt_index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            continue
        if attempt.get("outcome") == "invocation_failed":
            continue
        run_dir_raw = _text(attempt.get("run_dir"))
        if run_dir_raw is None:
            errors.append(f"research_attempt_run_dir_missing:{attempt_index}")
            continue
        run_dir = Path(run_dir_raw).resolve()
        artifacts_raw = attempt.get("attempt_artifacts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
        artifacts_by_kind = {
            str(artifact.get("kind")): artifact
            for artifact in artifacts
            if isinstance(artifact, dict) and _text(artifact.get("kind")) is not None
        }
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            kind = str(artifact.get("kind") or "unknown")
            path_raw = _text(artifact.get("path"))
            if path_raw is None:
                continue
            path = Path(path_raw).resolve()
            try:
                path.relative_to(run_dir)
            except ValueError:
                errors.append(f"research_attempt_artifact_outside_run_dir:{attempt_index}:{kind}")
                continue
            expected_exists = artifact.get("exists") is True
            if not expected_exists:
                if path.exists():
                    errors.append(f"research_attempt_artifact_appeared:{attempt_index}:{kind}")
                continue
            if not path.is_file():
                errors.append(f"research_attempt_artifact_missing:{attempt_index}:{kind}")
                continue
            if _sha256_path(path) != artifact.get("sha256") or path.stat().st_size != artifact.get(
                "size_bytes"
            ):
                errors.append(f"research_attempt_artifact_changed:{attempt_index}:{kind}")
        report_artifact = next(
            (
                artifact
                for artifact in artifacts
                if isinstance(artifact, dict) and artifact.get("kind") == "report"
            ),
            None,
        )
        if isinstance(report_artifact, dict) and report_artifact.get("path") != attempt.get(
            "report_path"
        ):
            errors.append(f"research_attempt_report_path_changed:{attempt_index}")

        target_artifact = artifacts_by_kind.get("target_ref")
        target_path_raw = (
            _text(target_artifact.get("path")) if isinstance(target_artifact, dict) else None
        )
        target_path = Path(target_path_raw) if target_path_raw is not None else None
        target_ref: dict[str, Any] | None = None
        if target_path is not None and target_path.is_file():
            try:
                target_raw = json.loads(target_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                target_raw = None
            if isinstance(target_raw, dict):
                target_ref = target_raw
            else:
                errors.append(f"research_attempt_target_ref_invalid:{attempt_index}")

        target_agent = (
            _text(target_ref.get("agent")).casefold() if isinstance(target_ref, dict) else None
        )
        attempt_kind = _text(attempt.get("attempt_kind"))
        if attempt_kind == "model_output_repair":
            if target_agent != "codex":
                errors.append(f"research_attempt_repair_agent_not_codex:{attempt_index}")
            expected_session = _text(attempt.get("agent_session_id"))
            resumed_session = _text(attempt.get("resumed_from_session_id"))
            requested_session = (
                _text(target_ref.get("requested_codex_resume_session_id"))
                if isinstance(target_ref, dict)
                else None
            )
            if (
                expected_session is None
                or resumed_session != expected_session
                or requested_session != expected_session
            ):
                errors.append(f"research_attempt_repair_session_provenance_invalid:{attempt_index}")

        if (
            target_agent == "codex"
            and attempt_kind in {"full_research", "model_output_repair", "fresh_research_retry"}
        ) or attempt_kind == "model_output_repair":
            auth_artifact = artifacts_by_kind.get("codex_subscription_auth")
            auth_path_raw = (
                _text(auth_artifact.get("path")) if isinstance(auth_artifact, dict) else None
            )
            auth_path = Path(auth_path_raw) if auth_path_raw is not None else None
            if (
                not isinstance(auth_artifact, dict)
                or auth_artifact.get("exists") is not True
                or auth_path is None
                or not auth_path.is_file()
            ):
                errors.append(f"research_attempt_codex_subscription_receipt_missing:{attempt_index}")
            else:
                errors.extend(
                    f"research_attempt_{attempt_index}_{error}"
                    for error in verify_controlled_codex_execpolicy_receipt(auth_path)
                )
    return errors


def verify_persisted_research_evidence(
    dossier: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Revalidate a retained runner receipt before any downstream planning stage."""
    errors: list[str] = []
    receipt_raw = dossier.get("evidence_verification")
    receipt = receipt_raw if isinstance(receipt_raw, dict) else {}
    if receipt.get("verification_method") != _VERIFICATION_METHOD:
        errors.append("research_receipt_method_invalid")
    if receipt.get("receipt_sha256") != evidence_verification_sha256(receipt):
        errors.append("research_receipt_hash_changed")
    if receipt.get("status") != "verified":
        errors.append("research_receipt_not_verified")
    if receipt.get("claims_sha256") != research_claims_sha256(dossier):
        errors.append("research_receipt_claims_changed")
    errors.extend(_persisted_research_attempt_errors(dossier))
    assignment_raw = dossier.get("evidence_assignment")
    assignment = assignment_raw if isinstance(assignment_raw, dict) else {}
    expected_ids_raw = assignment.get("expected_atom_ids")
    expected_ids = expected_ids_raw if isinstance(expected_ids_raw, list) else []
    errors.extend(_verify_assignment_files(assignment, expected_atom_ids=expected_ids))
    binding_errors: list[str] = []
    recomputed_bindings = _experiment_atom_bindings(
        dossier,
        assignment,
        errors=binding_errors,
    )
    errors.extend(binding_errors)
    if receipt.get("atom_bindings") != recomputed_bindings:
        errors.append("research_receipt_atom_bindings_changed")
    if receipt.get("assignment_sha256") != assignment.get("assignment_sha256"):
        errors.append("research_receipt_assignment_changed")
    for field in ("case_id", "problem_id", "repo_revision"):
        if receipt.get(field) != dossier.get(field):
            errors.append(f"research_receipt_{field}_changed")
    if _text(receipt.get("requested_repo_ref")) is None:
        errors.append("research_receipt_requested_ref_missing")
    if _text(receipt.get("resolved_repo_ref")) is None:
        errors.append("research_receipt_resolved_ref_missing")
    replay_isolation = receipt.get("replay_isolation")
    errors.extend(_isolation_receipt_errors(replay_isolation))

    planning_raw = _text(receipt.get("planning_workspace_dir"))
    planning_workspace = Path(planning_raw) if planning_raw is not None else None
    if planning_workspace is None or not planning_workspace.is_dir():
        errors.append("research_planning_workspace_unavailable")
    else:
        head = _workspace_head(planning_workspace)
        clean = _workspace_clean(planning_workspace)
        if (
            head != dossier.get("repo_revision")
            or head != receipt.get("planning_workspace_head")
            or clean is not True
            or receipt.get("planning_workspace_clean") is not True
        ):
            errors.append("research_planning_workspace_changed")

    research_raw = _text(receipt.get("workspace_dir"))
    research_workspace = Path(research_raw) if research_raw is not None else None
    if research_workspace is None or not research_workspace.is_dir():
        errors.append("research_workspace_unavailable")
    else:
        if _workspace_head(research_workspace) != receipt.get("workspace_head"):
            errors.append("research_workspace_head_changed")
        if planning_workspace is not None and planning_workspace.is_dir():
            overlay_errors, overlay = _workspace_overlay_errors(
                research_workspace=research_workspace,
                baseline_workspace=planning_workspace,
            )
            errors.extend(overlay_errors)
            if overlay != receipt.get("workspace_overlay"):
                errors.append("research_workspace_overlay_changed")

    run_dir_raw = _text(receipt.get("run_dir"))
    run_dir = Path(run_dir_raw) if run_dir_raw is not None else None
    events_path = run_dir / "normalized_events.jsonl" if run_dir is not None else None
    persisted_events: list[dict[str, Any]] = []
    if events_path is not None:
        persisted_events = _load_events(events_path, errors)
    errors.extend(
        _persisted_origin_attachment_errors(
            assignment=assignment,
            receipt=receipt,
            research_workspace=research_workspace,
            persisted_events=persisted_events,
        )
    )
    for hash_field, filename in (
        ("normalized_events_sha256", "normalized_events.jsonl"),
        ("run_report_sha256", "report.json"),
    ):
        path = run_dir / filename if run_dir is not None else None
        if path is None or not path.is_file() or receipt.get(hash_field) != _sha256_path(path):
            errors.append(f"research_runner_artifact_changed:{filename}")

    artifacts_raw = receipt.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            errors.append("research_artifact_receipt_invalid")
            continue
        artifact_id = _text(artifact.get("artifact_id"))
        if artifact_id is not None:
            artifacts_by_id[artifact_id] = artifact
        path_raw = _text(artifact.get("path"))
        path = Path(path_raw) if path_raw is not None else None
        if (
            path is None
            or not path.is_file()
            or artifact.get("sha256") != _sha256_path(path)
            or artifact.get("size_bytes") != path.stat().st_size
        ):
            errors.append(f"research_artifact_changed:{artifact.get('artifact_id')}")
    target_ref_artifact = artifacts_by_id.get("runner:target_ref")
    target_ref_path = (
        Path(str(target_ref_artifact.get("path")))
        if isinstance(target_ref_artifact, dict)
        else None
    )
    target_agent: str | None = None
    if target_ref_path is None or not target_ref_path.is_file():
        errors.append("research_target_ref_artifact_missing")
    else:
        try:
            target_ref = json.loads(target_ref_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            target_ref = None
        if not isinstance(target_ref, dict):
            errors.append("research_target_ref_artifact_invalid")
        else:
            target_agent_raw = target_ref.get("agent")
            target_agent = (
                target_agent_raw.strip().lower()
                if isinstance(target_agent_raw, str) and target_agent_raw.strip()
                else None
            )
            if target_agent is None:
                errors.append("research_target_ref_agent_missing")
            if target_ref.get("commit_sha") != dossier.get("repo_revision"):
                errors.append("research_target_ref_commit_changed")
            if target_ref.get("ref") != receipt.get("resolved_repo_ref"):
                errors.append("research_target_ref_resolved_ref_changed")

    if target_agent == "codex":
        auth_artifact = artifacts_by_id.get("runner:codex_subscription_auth")
        auth_path_raw = auth_artifact.get("path") if isinstance(auth_artifact, dict) else None
        auth_path = Path(auth_path_raw) if isinstance(auth_path_raw, str) else None
        if auth_path is None or not auth_path.is_file():
            errors.append("research_codex_subscription_receipt_missing")
        else:
            errors.extend(
                "research_" + error
                for error in verify_controlled_codex_execpolicy_receipt(auth_path)
            )

    declared_experiments_raw = dossier.get("experiments")
    declared_experiments = {
        str(experiment.get("experiment_id")): experiment
        for experiment in (
            declared_experiments_raw if isinstance(declared_experiments_raw, list) else []
        )
        if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
    }
    experiment_receipts_raw = receipt.get("experiments")
    experiment_receipts = (
        experiment_receipts_raw if isinstance(experiment_receipts_raw, list) else []
    )
    seen_replay_workspaces: set[str] = set()
    observed_signatures: dict[tuple[Any, ...], tuple[str, str]] = {}
    receipt_experiment_ids: set[str] = set()
    for experiment_receipt in experiment_receipts:
        if not isinstance(experiment_receipt, dict):
            errors.append("research_experiment_receipt_invalid")
            continue
        experiment_id = _text(experiment_receipt.get("experiment_id"))
        if experiment_id is None:
            errors.append("research_experiment_receipt_id_missing")
            continue
        receipt_experiment_ids.add(experiment_id)
        declared = declared_experiments.get(experiment_id)
        if declared is None:
            errors.append(f"research_experiment_not_declared:{experiment_id}")
            continue
        for field in (
            "scenario_kind",
            "addresses_atom_ids",
            "command",
            "outcome",
            "observable_assertion",
            "artifact_refs",
            "verification_boundary",
        ):
            if experiment_receipt.get(field) != declared.get(field):
                errors.append(f"research_experiment_receipt_changed:{experiment_id}:{field}")
        if experiment_receipt.get("declared_result") != declared.get("result"):
            errors.append(f"research_experiment_receipt_changed:{experiment_id}:result")
        if experiment_receipt.get("exit_code") != declared.get("exit_code"):
            errors.append(f"research_experiment_receipt_changed:{experiment_id}:exit_code")
        expected_authorized = (
            _authorized_replay_invocation(
                command=str(declared.get("command") or ""),
                experiment=declared,
                dossier=dossier,
                assignment=assignment,
                workspace=planning_workspace,
            )
            if planning_workspace is not None and planning_workspace.is_dir()
            else None
        )
        expected_argv = expected_authorized[0] if expected_authorized is not None else None
        expected_authorization = expected_authorized[1] if expected_authorized is not None else None
        if expected_argv is None or experiment_receipt.get("executed_argv") != expected_argv:
            errors.append(f"research_experiment_receipt_changed:{experiment_id}:executed_argv")
        if (
            expected_authorization is None
            or experiment_receipt.get("command_authorization") != expected_authorization
        ):
            errors.append(
                f"research_experiment_receipt_changed:{experiment_id}:command_authorization"
            )
        agent_event_index = experiment_receipt.get("agent_event_index")
        agent_event = (
            persisted_events[agent_event_index]
            if isinstance(agent_event_index, int)
            and not isinstance(agent_event_index, bool)
            and 0 <= agent_event_index < len(persisted_events)
            else None
        )
        agent_data = (
            agent_event.get("data")
            if isinstance(agent_event, dict) and isinstance(agent_event.get("data"), dict)
            else None
        )
        if (
            not isinstance(agent_event, dict)
            or agent_event.get("type") != "run_command"
            or experiment_receipt.get("agent_event_sha256") != _canonical_json_sha256(agent_event)
            or not isinstance(agent_data, dict)
            or _normalize_command(str(agent_data.get("command") or ""))
            != _normalize_command(str(declared.get("command") or ""))
            or agent_data.get("exit_code") != declared.get("exit_code")
        ):
            errors.append(f"research_agent_event_changed:{experiment_id}")
        else:
            output_excerpt = _text(agent_data.get("output_excerpt"))
            expected_excerpt_hash = (
                sha256(output_excerpt.encode()).hexdigest() if output_excerpt is not None else None
            )
            if experiment_receipt.get("agent_output_excerpt_sha256") != expected_excerpt_hash:
                errors.append(f"research_agent_event_output_changed:{experiment_id}")
        replay_workspace_raw = _text(experiment_receipt.get("workspace_dir"))
        replay_workspace = Path(replay_workspace_raw) if replay_workspace_raw is not None else None
        if (
            replay_workspace is None
            or not replay_workspace.is_dir()
            or _workspace_head(replay_workspace) != dossier.get("repo_revision")
        ):
            errors.append(f"research_replay_workspace_changed:{experiment_id}")
        elif replay_workspace_raw in seen_replay_workspaces:
            errors.append(f"research_replay_workspace_reused:{experiment_id}")
        else:
            seen_replay_workspaces.add(str(replay_workspace_raw))
            current_replay_state = _canonical_workspace_state(replay_workspace)
            current_replay_state_sha = _canonical_json_sha256(current_replay_state)
            replay_mutated = experiment_receipt.get("post_replay_mutations")
            transitions_raw = experiment_receipt.get("declared_state_transitions", [])
            transitions = transitions_raw if isinstance(transitions_raw, list) else []
            mutation_contract_valid = (
                isinstance(replay_mutated, bool)
                and experiment_receipt.get("undeclared_post_replay_mutations", []) == []
                and (
                    (
                        replay_mutated is False
                        and experiment_receipt.get("pre_replay_state_sha256")
                        == experiment_receipt.get("post_replay_state_sha256")
                    )
                    or (replay_mutated is True and bool(transitions))
                )
                and all(
                    isinstance(transition, dict)
                    and transition.get("runner_attested") is True
                    and transition.get("transition_sha256")
                    == _canonical_json_sha256(
                        {
                            key: value
                            for key, value in transition.items()
                            if key != "transition_sha256"
                        }
                    )
                    for transition in transitions
                )
            )
            if (
                experiment_receipt.get("post_replay_state_sha256") != current_replay_state_sha
                or not mutation_contract_valid
                or experiment_receipt.get("execution_isolation") != replay_isolation
            ):
                errors.append(f"research_replay_workspace_state_changed:{experiment_id}")
            for metadata_error in _execution_metadata_errors(
                experiment_receipt.get("execution_metadata"),
                isolation=replay_isolation if isinstance(replay_isolation, dict) else {},
            ):
                errors.append(f"research_replay_isolation_changed:{experiment_id}:{metadata_error}")
            overlay_raw = receipt.get("workspace_overlay")
            overlay = overlay_raw if isinstance(overlay_raw, dict) else {}
            if experiment_receipt.get("overlay_manifest_sha256") != overlay.get(
                "research_overlay_manifest_sha256"
            ):
                errors.append(f"research_replay_overlay_changed:{experiment_id}")

        stdout_artifact = artifacts_by_id.get(f"runner:replay:{experiment_id}:stdout")
        stderr_artifact = artifacts_by_id.get(f"runner:replay:{experiment_id}:stderr")
        stdout_path = (
            Path(str(stdout_artifact.get("path"))) if isinstance(stdout_artifact, dict) else None
        )
        stderr_path = (
            Path(str(stderr_artifact.get("path"))) if isinstance(stderr_artifact, dict) else None
        )
        if (
            stdout_path is None
            or stderr_path is None
            or not stdout_path.is_file()
            or not stderr_path.is_file()
            or experiment_receipt.get("stdout_path") != str(stdout_path)
            or experiment_receipt.get("stderr_path") != str(stderr_path)
        ):
            errors.append(f"research_replay_output_missing:{experiment_id}")
            continue
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        replay_exit_code = experiment_receipt.get("exit_code")
        assertion_rechecks = (
            isinstance(replay_exit_code, int)
            and not isinstance(replay_exit_code, bool)
            and _assert_observable(
                declared.get("observable_assertion", {}),
                exit_code=replay_exit_code,
                stdout=stdout,
                stderr=stderr,
            )
        )
        if (
            experiment_receipt.get("stdout_sha256") != _sha256_path(stdout_path)
            or experiment_receipt.get("stderr_sha256") != _sha256_path(stderr_path)
            or not assertion_rechecks
        ):
            errors.append(f"research_replay_assertion_changed:{experiment_id}")
        signature = (
            declared.get("command"),
            experiment_receipt.get("exit_code"),
            experiment_receipt.get("stdout_sha256"),
            experiment_receipt.get("stderr_sha256"),
        )
        previous = observed_signatures.get(signature)
        outcome = str(declared.get("outcome") or "")
        if previous is not None and previous[1] != outcome:
            errors.append(f"research_replay_outcome_conflict:{previous[0]}:{experiment_id}")
        observed_signatures[signature] = (experiment_id, outcome)
    if receipt_experiment_ids != set(declared_experiments):
        errors.append("research_experiment_receipt_coverage_mismatch")

    if receipt.get("origin_atom_ids") != expected_ids:
        errors.append("research_receipt_origin_atom_ids_changed")

    if planning_workspace is not None and planning_workspace.is_dir():
        files_raw = receipt.get("inspected_files")
        files = files_raw if isinstance(files_raw, list) else []
        persisted_file_paths: set[str] = set()
        persisted_observed_contents: dict[str, str] = {}
        for file_receipt in files:
            if not isinstance(file_receipt, dict):
                errors.append("research_inspected_file_receipt_invalid")
                continue
            path_raw = _text(file_receipt.get("path"))
            if path_raw is not None:
                persisted_file_paths.add(path_raw)
            path = planning_workspace / path_raw if path_raw is not None else None
            if (
                path is None
                or not path.is_file()
                or file_receipt.get("sha256") != _sha256_path(path)
                or file_receipt.get("git_blob_sha")
                != _git_blob_sha(planning_workspace, str(path_raw))
            ):
                errors.append(f"research_inspected_file_changed:{path_raw}")
                continue
            read_event_index = file_receipt.get("read_event_index")
            read_event = (
                persisted_events[read_event_index]
                if isinstance(read_event_index, int)
                and not isinstance(read_event_index, bool)
                and 0 <= read_event_index < len(persisted_events)
                else None
            )
            read_data = (
                read_event.get("data")
                if isinstance(read_event, dict) and isinstance(read_event.get("data"), dict)
                else None
            )
            expected_attestation = (
                observed_read_attestation(
                    path=path,
                    observed_text=read_data.get("observed_content"),
                    source_exit_code=(
                        read_data["source_exit_code"]
                        if isinstance(read_data.get("source_exit_code"), int)
                        and not isinstance(read_data.get("source_exit_code"), bool)
                        else -1
                    ),
                    allow_partial=read_data.get("read_source") == "tool",
                )
                if isinstance(read_data, dict) and path is not None and path.is_file()
                else {}
            )
            if (
                not isinstance(read_event, dict)
                or read_event.get("type") != "read_file"
                or file_receipt.get("read_event_sha256") != _canonical_json_sha256(read_event)
                or not isinstance(read_data, dict)
                or _normalize_path(str(read_data.get("path") or ""))
                != _normalize_path(path_raw or "")
                or read_data.get("content_observed") is not True
                or read_data.get("source_exit_code") != 0
                or read_data.get("read_source") != file_receipt.get("read_source")
                or read_data.get("bytes") != path.stat().st_size
                or read_data.get("file_size_bytes") != path.stat().st_size
                or read_data.get("file_sha256") != _sha256_path(path)
                or read_data.get("observed_bytes") != file_receipt.get("bytes_observed")
                or read_data.get("observed_content_sha256")
                != file_receipt.get("observed_content_sha256")
                or read_data.get("whole_file_observed") != file_receipt.get("whole_file_observed")
                or read_data.get("observed_start_line") != file_receipt.get("observed_start_line")
                or read_data.get("observed_end_line") != file_receipt.get("observed_end_line")
                or any(
                    read_data.get(field) != expected_attestation.get(field)
                    for field in (
                        "content_observed",
                        "whole_file_observed",
                        "observed_content",
                        "observed_content_sha256",
                        "observed_bytes",
                        "observed_start_line",
                        "observed_end_line",
                        "file_sha256",
                        "file_size_bytes",
                    )
                )
            ):
                errors.append(f"research_inspected_file_event_changed:{path_raw}")
            elif isinstance(read_data.get("observed_content"), str) and path_raw is not None:
                persisted_observed_contents[path_raw] = read_data["observed_content"]
        declared_file_paths = {
            str(path).replace("\\", "/").removeprefix("./")
            for path in dossier.get("inspected_files", [])
            if isinstance(path, str) and path.strip()
        }
        if persisted_file_paths != declared_file_paths:
            errors.append("research_inspected_file_receipt_coverage_mismatch")

        symbols_raw = receipt.get("inspected_symbols")
        symbols = symbols_raw if isinstance(symbols_raw, list) else []
        persisted_symbols: set[str] = set()
        for symbol_receipt in symbols:
            if not isinstance(symbol_receipt, dict):
                errors.append("research_inspected_symbol_receipt_invalid")
                continue
            symbol = _text(symbol_receipt.get("symbol"))
            symbol_path = _text(symbol_receipt.get("path"))
            if symbol is not None:
                persisted_symbols.add(symbol)
            if (
                symbol is None
                or symbol_path not in persisted_file_paths
                or not _symbol_definition_exists(
                    path=symbol_path,
                    content=persisted_observed_contents.get(symbol_path, ""),
                    symbol=symbol,
                )
            ):
                errors.append(f"research_inspected_symbol_changed:{symbol}")
        declared_symbols = {
            symbol.strip()
            for symbol in dossier.get("inspected_symbols", [])
            if isinstance(symbol, str) and symbol.strip()
        }
        if persisted_symbols != declared_symbols:
            errors.append("research_inspected_symbol_receipt_coverage_mismatch")
        persisted_replays = {
            str(experiment.get("experiment_id")): experiment
            for experiment in experiment_receipts
            if isinstance(experiment, dict) and _text(experiment.get("experiment_id")) is not None
        }
        causal_errors: list[str] = []
        recomputed_causal_links = _causal_link_receipts(
            dossier,
            clean_replays=persisted_replays,
            symbol_receipts=[symbol for symbol in symbols if isinstance(symbol, dict)],
            errors=causal_errors,
        )
        if receipt.get("causal_links") != recomputed_causal_links:
            errors.append("research_causal_links_changed")
        control_errors: list[str] = []
        recomputed_test_selections, recomputed_control_verifications = _causal_control_receipts(
            dossier,
            clean_replays=persisted_replays,
            planning_workspace=planning_workspace,
            symbol_receipts=[symbol for symbol in symbols if isinstance(symbol, dict)],
            errors=control_errors,
        )
        if control_errors:
            recomputed_test_selections = []
            recomputed_control_verifications = []
        if receipt.get("test_selections") != recomputed_test_selections:
            errors.append("research_test_selections_changed")
        if receipt.get("control_verifications") != recomputed_control_verifications:
            errors.append("research_control_verifications_changed")
        intervention_errors: list[str] = []
        recomputed_falsification_interventions = _falsification_intervention_receipts(
            dossier,
            clean_replays=persisted_replays,
            planning_workspace=planning_workspace,
            symbol_receipts=[symbol for symbol in symbols if isinstance(symbol, dict)],
            errors=intervention_errors,
        )
        if receipt.get("falsification_interventions") != recomputed_falsification_interventions:
            errors.append("research_falsification_interventions_changed")
        declared_experiments_raw = dossier.get("experiments")
        declared_experiments = {
            str(experiment.get("experiment_id")): experiment
            for experiment in (
                declared_experiments_raw
                if isinstance(declared_experiments_raw, list)
                else []
            )
            if isinstance(experiment, dict)
            and _text(experiment.get("experiment_id")) is not None
        }
        recomputed_adapter_receipts, recomputed_adapter_diagnostics = (
            _proof_adapter_receipts(
                dossier,
                case_id=str(dossier.get("case_id") or ""),
                problem_id=str(dossier.get("problem_id") or ""),
                experiments=declared_experiments,
                clean_replays=persisted_replays,
                evidence_assignment=assignment,
                atom_bindings=recomputed_bindings,
                planning_workspace=planning_workspace,
                symbol_receipts=[symbol for symbol in symbols if isinstance(symbol, dict)],
                artifact_receipts=[
                    artifact
                    for artifact in receipt.get("artifacts", [])
                    if isinstance(artifact, dict)
                ],
                inspected_file_receipts=[
                    file_receipt for file_receipt in files if isinstance(file_receipt, dict)
                ],
            )
        )
        if receipt.get("proof_adapter_receipts", []) != recomputed_adapter_receipts:
            errors.append("research_proof_adapter_receipts_changed")
        if receipt.get("proof_adapter_diagnostics", []) != recomputed_adapter_diagnostics:
            errors.append("research_proof_adapter_diagnostics_changed")
        if intervention_errors and not recomputed_adapter_receipts:
            errors.extend(intervention_errors)
        preliminary_mechanism_errors: list[str] = []
        preliminary_mechanism_evidence = _typed_mechanism_evidence_receipts(
            dossier,
            clean_replays=persisted_replays,
            symbol_receipts=[symbol for symbol in symbols if isinstance(symbol, dict)],
            causal_links=recomputed_causal_links,
            strong_controls=recomputed_control_verifications,
            falsification_interventions=recomputed_falsification_interventions,
            deterministic_closures=[],
            atom_bindings=recomputed_bindings,
            errors=preliminary_mechanism_errors,
            proof_adapter_receipts=recomputed_adapter_receipts,
        )
        recomputed_deterministic_closures = _deterministic_mechanism_closure_receipts(
            dossier,
            clean_replays=persisted_replays,
            symbol_receipts=[symbol for symbol in symbols if isinstance(symbol, dict)],
            mechanism_evidence=preliminary_mechanism_evidence,
        )
        if receipt.get("deterministic_mechanism_closures") != recomputed_deterministic_closures:
            errors.append("research_deterministic_mechanism_closures_changed")
        mechanism_errors: list[str] = []
        recomputed_mechanism_evidence = _typed_mechanism_evidence_receipts(
            dossier,
            clean_replays=persisted_replays,
            symbol_receipts=[symbol for symbol in symbols if isinstance(symbol, dict)],
            causal_links=recomputed_causal_links,
            strong_controls=recomputed_control_verifications,
            falsification_interventions=recomputed_falsification_interventions,
            deterministic_closures=recomputed_deterministic_closures,
            proof_adapter_receipts=recomputed_adapter_receipts,
            atom_bindings=recomputed_bindings,
            errors=mechanism_errors,
        )
        errors.extend(mechanism_errors)
        if receipt.get("mechanism_evidence") != recomputed_mechanism_evidence:
            errors.append("research_mechanism_evidence_changed")
        (
            recomputed_verified_mechanism,
            recomputed_verified_mechanism_sha256,
            recomputed_verified_mechanism_provenance,
            recomputed_verified_mechanism_provenance_sha256,
        ) = _verified_mechanism_projection(
            dossier,
            mechanism_evidence=recomputed_mechanism_evidence,
            control_verifications=recomputed_control_verifications,
            falsification_interventions=recomputed_falsification_interventions,
            deterministic_closures=recomputed_deterministic_closures,
        )
        if receipt.get("verified_mechanism") != recomputed_verified_mechanism:
            errors.append("research_verified_mechanism_changed")
        if receipt.get("verified_mechanism_sha256") != recomputed_verified_mechanism_sha256:
            errors.append("research_verified_mechanism_hash_changed")
        if receipt.get("verified_mechanism_provenance") != recomputed_verified_mechanism_provenance:
            errors.append("research_verified_mechanism_provenance_changed")
        if (
            receipt.get("verified_mechanism_provenance_sha256")
            != recomputed_verified_mechanism_provenance_sha256
        ):
            errors.append("research_verified_mechanism_provenance_hash_changed")
        oracle_errors: list[str] = []
        run_dir_raw = _text(receipt.get("run_dir"))
        overlay_raw = receipt.get("workspace_overlay")
        overlay = overlay_raw if isinstance(overlay_raw, dict) else {}
        recomputed_outcome_oracles = _outcome_oracle_receipts(
            dossier,
            clean_replays=persisted_replays,
            mechanism_evidence=recomputed_mechanism_evidence,
            proof_adapter_receipts=recomputed_adapter_receipts,
            verified_mechanism=recomputed_verified_mechanism,
            verified_mechanism_sha256=recomputed_verified_mechanism_sha256,
            verified_mechanism_provenance=recomputed_verified_mechanism_provenance,
            verified_mechanism_provenance_sha256=(recomputed_verified_mechanism_provenance_sha256),
            control_verifications=recomputed_control_verifications,
            falsification_interventions=recomputed_falsification_interventions,
            inspected_file_receipts=[
                file_receipt for file_receipt in files if isinstance(file_receipt, dict)
            ],
            inspected_symbol_receipts=[
                symbol_receipt for symbol_receipt in symbols if isinstance(symbol_receipt, dict)
            ],
            evidence_assignment=assignment,
            atom_bindings=recomputed_bindings,
            planning_workspace=planning_workspace,
            research_workspace=research_workspace,
            overlay_manifest=(
                overlay.get("research_overlay_manifest", {})
                if isinstance(overlay.get("research_overlay_manifest"), dict)
                else {}
            ),
            run_dir=Path(run_dir_raw) if run_dir_raw is not None else Path("."),
            repo_revision=_text(dossier.get("repo_revision")),
            errors=oracle_errors,
        )
        errors.extend(oracle_errors)
        if receipt.get("outcome_oracles") != recomputed_outcome_oracles:
            errors.append("research_outcome_oracles_changed")
        recomputed_verification_boundaries, boundary_errors = (
            _verification_boundary_receipts(
                experiments=declared_experiments,
                clean_replays=persisted_replays,
                mechanism_evidence=recomputed_mechanism_evidence,
                proof_adapter_receipts=recomputed_adapter_receipts,
                outcome_oracles=recomputed_outcome_oracles,
                verified_mechanism_provenance=(
                    recomputed_verified_mechanism_provenance
                ),
            )
        )
        errors.extend(boundary_errors)
        if receipt.get("verification_boundaries", []) != recomputed_verification_boundaries:
            errors.append("research_verification_boundaries_changed")
        falsification_errors: list[str] = []
        recomputed_falsification_attempts = _falsification_attempt_receipts(
            dossier,
            clean_replays=persisted_replays,
            mechanism_evidence=recomputed_mechanism_evidence,
            falsification_interventions=recomputed_falsification_interventions,
            deterministic_closures=recomputed_deterministic_closures,
            proof_adapter_receipts=recomputed_adapter_receipts,
            errors=falsification_errors,
        )
        errors.extend(falsification_errors)
        hypothesis_errors: list[str] = []
        recomputed_hypotheses = _hypothesis_receipts(
            dossier,
            experiment_outcomes={
                experiment_id: str(experiment.get("outcome") or "")
                for experiment_id, experiment in declared_experiments.items()
            },
            artifact_keys=set(artifacts_by_id),
            falsification_attempts=recomputed_falsification_attempts,
            errors=hypothesis_errors,
        )
        errors.extend(hypothesis_errors)
        if receipt.get("hypothesis_refs") != recomputed_hypotheses:
            errors.append("research_hypothesis_receipts_changed")
        failure_path_errors: list[str] = []
        recomputed_failure_paths = _failure_path_receipts(
            dossier,
            test_selections=recomputed_test_selections,
            control_verifications=recomputed_control_verifications,
            errors=failure_path_errors,
        )
        errors.extend(failure_path_errors)
        if receipt.get("failure_paths") != recomputed_failure_paths:
            errors.append("research_failure_paths_changed")
    bundle_raw = dossier.get("post_research_same_mechanism_bundle")
    if bundle_raw is not None:
        bundle = bundle_raw if isinstance(bundle_raw, dict) else {}
        supplied_hash = bundle.get("bundle_sha256")
        projection = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
        if supplied_hash != _canonical_json_sha256(projection):
            errors.append("research_post_relation_bundle_hash_changed")
        members_raw = bundle.get("member_research_dossiers")
        members = members_raw if isinstance(members_raw, list) else []
        if len(members) < 2 or any(not isinstance(member, dict) for member in members):
            errors.append("research_post_relation_bundle_members_invalid")
        else:
            for index, member in enumerate(members):
                member_ready, member_errors = verify_persisted_research_evidence(member)
                if not member_ready:
                    errors.extend(
                        f"research_post_relation_member_invalid:{index}:{error}"
                        for error in member_errors
                    )
    return not errors, list(dict.fromkeys(errors))


__all__ = [
    "materialize_clean_revision_view",
    "verify_persisted_research_evidence",
    "verify_research_evidence",
]
