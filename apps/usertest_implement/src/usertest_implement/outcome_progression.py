from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backlog_repo import (
    STALE_OUTCOME_BLOCKER_RISK_PREFIX,
    extract_outcome_markdown,
    verify_outcome_record_provenance,
)
from backlog_repo.ticket_provenance import is_generated_backlog_ticket
from runner_core import run_outcome_evidence_role

from usertest_implement.ledger import transition_outcome_files
from usertest_implement.outcome_evidence import validate_bound_outcome_role_receipt
from usertest_implement.selection import (
    _select_review_ticket,
    _selected_ticket_provenance,
)
from usertest_implement.tickets import parse_ticket_markdown_metadata

_BEFORE_AFTER_HEADING = "Original-scenario before / after proof"
_EXPECTED_OUTCOME_STATES = frozenset({"resolved", "mitigated"})
_TERMINAL_IMPLEMENTATION_STATES = frozenset({"resolved", "mitigated"})
_PROGRESSIBLE_OUTCOME_STATES = frozenset(
    {
        "implemented",
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
        "unverified",
    }
)
_RECURRENCE_RISK = (
    "Recurrence has not yet been observed in a later source-evidence window."
)
_KNOWN_SATISFIED_RISKS = frozenset(
    {
        "Original failure scenario has not been replayed after merge",
        "Live runtime verification is still required",
    }
)
_OUTCOME_WORKTREES_ROOT_ENV = "USERTEST_IMPLEMENT_OUTCOME_WORKTREES_ROOT"
_OUTCOME_TRUSTED_RUNS_ROOT_ENV = "USERTEST_IMPLEMENT_OUTCOME_TRUSTED_RUNS_ROOT"


@dataclass(frozen=True)
class OutcomeProgressionResult:
    fingerprint: str
    ticket_path: Path
    starting_state: str
    final_state: str
    expected_state: str | None
    status: str
    roles_run: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == "complete" and self.final_state == self.expected_state

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ticket_path"] = str(self.ticket_path)
        payload["complete"] = self.complete
        return payload


class OutcomeRoleDidNotPass(RuntimeError):
    def __init__(self, *, role: str, artifact_path: Path, timed_out: bool) -> None:
        self.role = role
        self.artifact_path = artifact_path
        self.timed_out = timed_out
        reason = "timed out" if timed_out else "did not satisfy its causal predicates"
        super().__init__(
            f"Post-merge {role} role {reason}; retained artifact: {artifact_path}"
        )


class OutcomeContractNotExecutable(RuntimeError):
    """A mandatory, recognized outcome command cannot run on the reviewed head."""

    def __init__(
        self,
        *,
        receipt_path: Path,
        failures: list[dict[str, Any]],
    ) -> None:
        self.receipt_path = receipt_path
        self.failures = tuple(dict(item) for item in failures)
        first = self.failures[0] if self.failures else {}
        role = str(first.get("role") or "unknown")
        command = str(first.get("command") or "unknown")
        reason = str(first.get("reason") or "mandatory command is not executable")
        remaining = len(self.failures) - 1
        suffix = f"; {remaining} additional failure(s)" if remaining > 0 else ""
        super().__init__(
            "Mandatory outcome command is not executable on the exact reviewed head: "
            f"role={role}; command={command!r}; reason={reason}{suffix}; "
            f"retained receipt: {receipt_path}"
        )


def _resolve_outcome_worktrees_root(*, repo_root: Path) -> Path:
    """Resolve the disposable outcome checkout root without hiding host policy.

    The default remains beneath the controller repository for compatibility. Hosts
    whose controller volume is constrained can explicitly place the short-lived
    detached worktrees on another volume. Relative overrides are rejected so the
    effective storage boundary is unambiguous in operational commands and logs.
    """

    configured = os.environ.get(_OUTCOME_WORKTREES_ROOT_ENV)
    if configured is None or not configured.strip():
        return (repo_root / ".tmp" / "outcome_worktrees").resolve()
    candidate = Path(configured.strip()).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{_OUTCOME_WORKTREES_ROOT_ENV} must be an absolute path")
    return candidate.resolve()


def _resolve_outcome_trusted_runs_root(*, repo_root: Path) -> Path:
    """Resolve the retained oracle-asset root without creating or probing it.

    Outcome-role outputs and their trusted receipt boundary remain beneath the
    controller repository. This optional host override is only for retained oracle
    assets that may live on another volume. Downstream asset validation remains
    responsible for existence, containment, and provenance checks.
    """

    configured = os.environ.get(_OUTCOME_TRUSTED_RUNS_ROOT_ENV)
    if configured is None or not configured.strip():
        return (repo_root / "runs").resolve()
    candidate = Path(configured.strip()).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{_OUTCOME_TRUSTED_RUNS_ROOT_ENV} must be an absolute path")
    return candidate.resolve()


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _parse_json_section(markdown: str, *, heading: str) -> dict[str, Any]:
    pattern = re.compile(
        rf"^###\s+{re.escape(heading)}\s*$"
        r".*?^```json\s*$\n(?P<payload>.*?)^```\s*$",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown)
    if match is None:
        raise ValueError(
            "Generated plan is missing its hashed before/after reproduction section"
        )
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Generated plan before/after reproduction section is invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("Generated plan before/after reproduction must be an object")
    return payload


def expected_outcome_state_from_markdown(markdown: str) -> str:
    """Read the plan-authorized outcome without trusting mutable CLI arguments.

    The section is part of the canonical local-plan and ticket-body hashes already
    checked by ``_selected_ticket_provenance``.  Post-merge progression therefore
    follows the researched plan's claim (resolution versus mitigation), not a new
    classification invented after implementation.
    """

    reproduction = _parse_json_section(markdown, heading=_BEFORE_AFTER_HEADING)
    raw = reproduction.get("expected_outcome_state")
    state = raw.strip().lower() if isinstance(raw, str) else ""
    if state not in _EXPECTED_OUTCOME_STATES:
        raise ValueError(
            "Generated plan before_after_reproduction.expected_outcome_state must be "
            "resolved or mitigated"
        )
    return state


def _git(
    repository: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git without imposing an arbitrary wall-clock timeout."""

    repository = repository.expanduser().resolve()
    proc = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository.as_posix()}",
            "-C",
            str(repository),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            f"git {' '.join(args)} failed for {repository}"
            + (f": {detail}" if detail else "")
        )
    return proc


def _resolve_merged_commit(repository: Path, merged_commit: str) -> str:
    requested = merged_commit.strip()
    proc = _git(repository, "rev-parse", "--verify", f"{requested}^{{commit}}", check=False)
    if proc.returncode != 0:
        # The GitHub merge can succeed before the local object database sees the
        # merge commit. Fetch the exact object, without changing the caller's branch.
        _git(repository, "fetch", "--no-tags", "origin", requested)
        proc = _git(repository, "rev-parse", "--verify", f"{requested}^{{commit}}")
    resolved = (proc.stdout or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", resolved) is None:
        raise RuntimeError(f"Merged commit did not resolve to a Git commit: {requested}")
    return resolved


@contextmanager
def clean_merged_commit_worktree(
    *,
    repository: Path,
    merged_commit: str,
    worktrees_root: Path,
    fingerprint: str,
) -> Iterator[Path]:
    """Yield a clean detached checkout of exactly one merged commit and remove it."""

    repository = repository.expanduser().resolve()
    worktrees_root = worktrees_root.expanduser().resolve()
    worktrees_root.mkdir(parents=True, exist_ok=True)
    resolved_commit = _resolve_merged_commit(repository, merged_commit)
    workspace = (
        worktrees_root / f"{fingerprint}-{resolved_commit[:12]}-{uuid.uuid4().hex[:10]}"
    ).resolve()
    if not workspace.is_relative_to(worktrees_root):
        raise RuntimeError("Refusing to create an outcome worktree outside its controlled root")
    added = False
    try:
        _git(repository, "worktree", "add", "--detach", str(workspace), resolved_commit)
        added = True
        observed_head = (_git(workspace, "rev-parse", "HEAD").stdout or "").strip().lower()
        if observed_head != resolved_commit:
            raise RuntimeError(
                "Outcome worktree is not at the merged commit: "
                f"expected={resolved_commit} observed={observed_head}"
            )
        status = _git(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if (status.stdout or "").strip():
            raise RuntimeError("Outcome worktree was not clean immediately after checkout")
        yield workspace
    finally:
        if added:
            _git(repository, "worktree", "remove", "--force", str(workspace), check=False)
        if workspace.exists() and workspace.is_relative_to(worktrees_root):
            shutil.rmtree(workspace, ignore_errors=True)
        _git(repository, "worktree", "prune", check=False)


def _load_current(ticket_path: Path) -> dict[str, Any]:
    current = extract_outcome_markdown(ticket_path.read_text(encoding="utf-8"))
    if current is None:
        raise ValueError(f"Ticket has no durable outcome record: {ticket_path}")
    return current


def _passing_evidence(current: dict[str, Any], field: str) -> bool:
    raw = current.get(field)
    return isinstance(raw, list) and any(
        isinstance(item, dict)
        and str(item.get("result") or "").strip().lower() == "passed"
        for item in raw
    )


def _requires_live_from_markdown(markdown: str) -> bool:
    raw = parse_ticket_markdown_metadata(markdown).get("requires_live_verification")
    normalized = raw.strip().lower() if isinstance(raw, str) else ""
    if normalized not in {"true", "false"}:
        raise ValueError(
            "Generated plan Requires live verification metadata must be true or false"
        )
    return normalized == "true"


def _validate_outcome_role_alignment(
    *,
    ticket_markdown: str,
    current: dict[str, Any],
    selected_provenance: dict[str, Any],
    expected_state: str,
) -> None:
    """Reject internally inconsistent outcome classifications, not broad PRs.

    The generated ticket, durable outcome, and runner-owned role contract are three
    hashed views of the same researched claim.  A local-only replay must not resolve
    a plan whose contract declares a live boundary, and a mitigation contract must
    not be silently promoted to resolution (or vice versa).
    """

    metadata_requires_live = _requires_live_from_markdown(ticket_markdown)
    outcome_requires_live = current.get("requires_live_verification")
    if outcome_requires_live is not metadata_requires_live:
        raise ValueError(
            "Durable outcome live-verification classification disagrees with the "
            "generated plan metadata"
        )
    contract = selected_provenance.get("verification_contract")
    roles = contract.get("outcome_roles") if isinstance(contract, dict) else None
    if not isinstance(roles, dict):
        raise ValueError("Selected stage-6 plan has no outcome-role contract")
    if not isinstance(roles.get("original_scenario"), dict):
        raise ValueError("Selected stage-6 plan has no original-scenario outcome role")
    has_live_role = isinstance(roles.get("live"), dict)
    if has_live_role is not metadata_requires_live:
        raise ValueError(
            "Selected stage-6 live role disagrees with Requires live verification"
        )
    has_mitigation_role = isinstance(roles.get("mitigation_effect"), dict)
    expects_mitigation = expected_state == "mitigated"
    if has_mitigation_role is not expects_mitigation:
        raise ValueError(
            "Selected stage-6 mitigation role disagrees with expected outcome state"
        )


def _has_bound_passing_role_evidence(
    *,
    current: dict[str, Any],
    field: str,
    role: str,
    selected_provenance: dict[str, Any],
    trusted_runs_root: Path,
) -> bool:
    """Return true only for a retained role artifact that still validates exactly."""

    raw = current.get(field)
    if not isinstance(raw, list):
        return False
    bound: dict[str, Any] | None = None
    for item in raw:
        if (
            not isinstance(item, dict)
            or str(item.get("result") or "").strip().lower() != "passed"
        ):
            continue
        receipt = item.get("runner_receipt")
        if not isinstance(receipt, dict):
            continue
        artifact_path = receipt.get("role_artifact_path")
        artifact_sha256 = receipt.get("role_artifact_sha256")
        if not isinstance(artifact_path, str) or not isinstance(artifact_sha256, str):
            continue
        if bound is None:
            bound = _bound_provenance(selected_provenance, current)
        implementation_commit, execution_commit, amendment_id = (
            _verification_execution_provenance(current)
        )
        try:
            revalidated = validate_bound_outcome_role_receipt(
                role_artifact_path=Path(artifact_path),
                evidence_kind=role,
                case_id=str(current["case_id"]),
                plan_revision_id=str(current["plan_revision_id"]),
                merged_commit=implementation_commit,
                expected_ticket_provenance=bound,
                trusted_runs_root=trusted_runs_root,
                expected_role_artifact_sha256=artifact_sha256,
                execution_commit=execution_commit,
                verification_amendment_id=amendment_id,
            )
        except (OSError, ValueError):
            continue
        if all(receipt.get(key) == value for key, value in revalidated.items()):
            return True
    return False


def _require_terminal_outcome_provenance(
    *,
    current: dict[str, Any],
    repo_root: Path,
    owner_root: Path,
) -> None:
    target_branch = str(current.get("target_branch") or "").strip()
    if target_branch:
        # A GitHub merge updates the remote before this long-lived supervisor's
        # tracking ref. Refresh only the recorded target branch so ancestry checks
        # evaluate the actual merged state rather than a stale local snapshot.
        _git(
            owner_root,
            "fetch",
            "--no-tags",
            "origin",
            f"{target_branch}:refs/remotes/origin/{target_branch}",
            check=False,
        )
    trusted_runs_roots: list[Path] = []
    for candidate in (
        (repo_root / "runs").resolve(),
        _resolve_outcome_trusted_runs_root(repo_root=repo_root),
    ):
        if candidate not in trusted_runs_roots:
            trusted_runs_roots.append(candidate)
    verification = verify_outcome_record_provenance(
        current,
        trusted_runs_roots=trusted_runs_roots,
        owner_roots=[owner_root.resolve()],
    )
    if verification.get("verified") is not True:
        errors_raw = verification.get("errors")
        errors = [str(item) for item in errors_raw] if isinstance(errors_raw, list) else []
        detail = "; ".join(errors[:8]) or "retained provenance did not verify"
        raise ValueError(f"Durable terminal outcome provenance failed: {detail}")


def _merge_evidence(current: dict[str, Any], field: str, item: dict[str, Any]) -> list[Any]:
    existing_raw = current.get(field)
    existing = list(existing_raw) if isinstance(existing_raw, list) else []
    marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
    if marker not in {
        json.dumps(value, sort_keys=True, ensure_ascii=False)
        for value in existing
        if isinstance(value, dict)
    }:
        existing.append(item)
    return existing


def _remaining_risks_after_role(
    current: dict[str, Any],
    *,
    completed_role: str,
) -> list[str]:
    raw = current.get("remaining_risks")
    risks = [str(item).strip() for item in raw] if isinstance(raw, list) else []
    kept: list[str] = []
    for risk in risks:
        if risk in _KNOWN_SATISFIED_RISKS and (
            completed_role == "live" or risk.startswith("Original failure")
        ):
            continue
        if risk.startswith(f"Post-merge {completed_role} verification did not pass"):
            continue
        # This prefix is runner-owned transient blockage, not a researched plan
        # residual.  Reaching this function means a required role has now passed,
        # so any earlier setup, transport, or role-specific blocker is stale even
        # when its detail did not contain the eventual role name.
        if risk.startswith(STALE_OUTCOME_BLOCKER_RISK_PREFIX):
            continue
        if risk and risk not in kept:
            kept.append(risk)
    return kept


def _transition(
    *,
    ledger_path: Path,
    ticket_path: Path,
    fingerprint: str,
    state: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    return transition_outcome_files(
        ledger_path=ledger_path,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
        state=state,
        recorded_at=_utc_now_z(),
        updates=updates,
    )


def _record_blocked_progression(
    *,
    ledger_path: Path,
    ticket_path: Path,
    fingerprint: str,
    detail: str,
) -> dict[str, Any]:
    current = _load_current(ticket_path)
    if current["state"] == "unverified" or current["state"] not in (
        _PROGRESSIBLE_OUTCOME_STATES - {"unverified"}
    ):
        return current
    risks_raw = current.get("remaining_risks")
    risks = [str(item) for item in risks_raw] if isinstance(risks_raw, list) else []
    risk = f"Post-merge outcome verification is blocked: {detail}"
    if risk not in risks:
        risks.append(risk)
    return _transition(
        ledger_path=ledger_path,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
        state="unverified",
        updates={"remaining_risks": risks},
    )


def _bound_provenance(
    selected_provenance: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    ticket_provenance = current.get("ticket_provenance")
    if not isinstance(ticket_provenance, dict):
        raise ValueError("Durable outcome ticket provenance is missing")
    verified_head = ticket_provenance.get("verified_implementation_head")
    if not isinstance(verified_head, str) or not verified_head.strip():
        raise ValueError("Durable outcome is missing its verified implementation head")
    return {**selected_provenance, "verified_implementation_head": verified_head.strip()}


def _verification_execution_provenance(
    current: dict[str, Any],
) -> tuple[str, str, str | None]:
    implementation_commit = str(current.get("merged_commit") or "").strip().casefold()
    if not implementation_commit:
        raise ValueError("Durable outcome is missing merged_commit")
    amendment = current.get("verification_amendment")
    if not isinstance(amendment, dict):
        return implementation_commit, implementation_commit, None
    execution_commit = str(amendment.get("verification_commit") or "").strip().casefold()
    amendment_id = str(amendment.get("amendment_id") or "").strip()
    if not execution_commit or not amendment_id:
        raise ValueError("Durable outcome verification amendment is incomplete")
    return implementation_commit, execution_commit, amendment_id


def _run_role(
    *,
    role: str,
    workspace: Path,
    output_path: Path,
    current: dict[str, Any],
    selected_provenance: dict[str, Any],
    trusted_runs_root: Path,
    role_runner: Callable[..., dict[str, Any]],
    trusted_oracle_assets_root: Path | None = None,
) -> dict[str, Any]:
    contract = selected_provenance.get("verification_contract")
    roles = contract.get("outcome_roles") if isinstance(contract, dict) else None
    role_contract = roles.get(role) if isinstance(roles, dict) else None
    if not isinstance(role_contract, dict):
        raise ValueError(f"Selected stage-6 plan does not define outcome role: {role}")
    bound = _bound_provenance(selected_provenance, current)
    implementation_commit, execution_commit, amendment_id = (
        _verification_execution_provenance(current)
    )
    runner_kwargs: dict[str, Any] = {}
    if isinstance(role_contract.get("oracle"), dict):
        runner_kwargs["trusted_oracle_assets_root"] = trusted_oracle_assets_root
    if amendment_id is not None:
        runner_kwargs["execution_commit"] = execution_commit
        runner_kwargs["verification_amendment_id"] = amendment_id
    artifact = role_runner(
        workspace=workspace,
        output_path=output_path,
        role=role,
        role_contract=role_contract,
        case_id=str(current["case_id"]),
        plan_revision_id=str(current["plan_revision_id"]),
        merged_commit=implementation_commit,
        verification_contract_sha256=str(
            selected_provenance["verification_contract_sha256"]
        ),
        target_contract_sha256=str(selected_provenance["target_contract_sha256"]),
        verified_implementation_head=str(bound["verified_implementation_head"]),
        timeout_seconds=None,
        **runner_kwargs,
    )
    if artifact.get("passed") is not True:
        raise OutcomeRoleDidNotPass(
            role=role,
            artifact_path=output_path,
            timed_out=artifact.get("timed_out") is True,
        )
    receipt = validate_bound_outcome_role_receipt(
        role_artifact_path=output_path,
        evidence_kind=role,
        case_id=str(current["case_id"]),
        plan_revision_id=str(current["plan_revision_id"]),
        merged_commit=implementation_commit,
        expected_ticket_provenance=bound,
        trusted_runs_root=trusted_runs_root,
        **(
            {
                "execution_commit": execution_commit,
                "verification_amendment_id": amendment_id,
            }
            if amendment_id is not None
            else {}
        ),
    )
    return {
        "kind": "runner_outcome_role",
        "reference": str(output_path),
        "result": "passed",
        "outcome_oracle_id": receipt.get("outcome_oracle_id"),
        "proof_scope": receipt.get("proof_scope"),
        "runner_receipt": receipt,
    }


def _mandatory_premerge_outcome_roles(
    *,
    requires_live: bool,
    expected_state: str,
) -> tuple[str, ...]:
    roles = ["original_scenario"]
    if requires_live:
        roles.append("live")
    if expected_state == "mitigated":
        roles.append("mitigation_effect")
    return tuple(roles)


def _python_pytest_node_targets(command: str) -> tuple[str, ...] | None:
    """Return pytest node targets only for the narrow command form we understand.

    Other command forms remain a semantic-review concern. This preflight must not
    turn parser incompleteness into a hard merge gate.
    """

    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    executable = Path(argv[0]).name.casefold()
    if not (
        executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}
        or re.fullmatch(r"python\d+(?:\.\d+)?(?:\.exe)?", executable)
    ):
        return None
    try:
        module_index = argv.index("-m")
    except ValueError:
        return None
    if module_index + 1 >= len(argv) or argv[module_index + 1] != "pytest":
        return None
    targets = tuple(
        token
        for token in argv[module_index + 2 :]
        if "::" in token and token.split("::", 1)[0].casefold().endswith(".py")
    )
    return targets or None


def _static_python_node_present(path: Path, node_id: str) -> bool | None:
    """Return true/false for a statically decidable node, else unknown."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return None
    parts = [part.split("[", 1)[0] for part in node_id.split("::")]
    if not parts or any(not part for part in parts):
        return None
    body: list[ast.stmt] = tree.body
    for index, part in enumerate(parts):
        match = next(
            (
                node
                for node in body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ),
            None,
        )
        if match is None:
            return False
        if index < len(parts) - 1:
            if not isinstance(match, ast.ClassDef):
                return False
            body = match.body
    return True


def _collect_only_node_status(*, workspace: Path, target: str) -> dict[str, Any]:
    """Disambiguate dynamically generated nodes without executing their test bodies."""

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", target],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {
            "status": "reviewable_unknown",
            "detail": f"collect-only could not start: {type(exc).__name__}: {exc}",
        }
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode == 0:
        return {
            "status": "verified_collect_only",
            "collect_only_exit_code": 0,
        }
    diagnostic = f"{stdout}\n{stderr}".casefold()
    definite_missing = "not found:" in diagnostic and (
        "no match in any of" in diagnostic or "found no collectors" in diagnostic
    )
    if definite_missing:
        return {
            "status": "correction_required",
            "reason": "pytest_node_missing",
            "collect_only_exit_code": int(proc.returncode),
            "collect_only_output_excerpt": (stdout + stderr)[-4000:],
        }
    return {
        "status": "reviewable_unknown",
        "detail": "collect-only did not establish whether the node exists",
        "collect_only_exit_code": int(proc.returncode),
        "collect_only_output_excerpt": (stdout + stderr)[-4000:],
    }


def _inspect_python_pytest_target(
    *,
    workspace: Path,
    target: str,
) -> dict[str, Any]:
    path_text, node_id = target.split("::", 1)
    candidate = Path(path_text)
    if candidate.is_absolute():
        return {
            "status": "correction_required",
            "reason": "pytest_target_outside_workspace",
            "path": path_text,
            "node_id": node_id,
        }
    resolved = (workspace / candidate).resolve()
    if not resolved.is_relative_to(workspace):
        return {
            "status": "correction_required",
            "reason": "pytest_target_outside_workspace",
            "path": path_text,
            "node_id": node_id,
        }
    relative_path = resolved.relative_to(workspace).as_posix()
    if not resolved.is_file():
        return {
            "status": "correction_required",
            "reason": "pytest_target_path_missing",
            "path": relative_path,
            "node_id": node_id,
        }
    static_present = _static_python_node_present(resolved, node_id)
    if static_present is True:
        return {
            "status": "verified_static",
            "path": relative_path,
            "node_id": node_id,
        }
    collected = _collect_only_node_status(workspace=workspace, target=target)
    return {
        **collected,
        "path": relative_path,
        "node_id": node_id,
        **(
            {"static_result": "missing"}
            if static_present is False
            else {"static_result": "unresolved"}
        ),
    }


def _write_outcome_executability_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _require_mandatory_outcome_commands_executable(
    *,
    workspace: Path,
    selected_provenance: dict[str, Any],
    mandatory_roles: tuple[str, ...],
    verified_implementation_head: str,
    receipt_path: Path,
) -> dict[str, Any]:
    """Check recognized mandatory commands on the exact clean reviewed head."""

    workspace = workspace.expanduser().resolve()
    contract = selected_provenance.get("verification_contract")
    roles_raw = contract.get("outcome_roles") if isinstance(contract, dict) else None
    roles = roles_raw if isinstance(roles_raw, dict) else {}
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for role in mandatory_roles:
        role_contract = roles.get(role)
        commands_raw = role_contract.get("commands") if isinstance(role_contract, dict) else None
        commands = commands_raw if isinstance(commands_raw, list) else []
        if not commands:
            checks.append(
                {
                    "role": role,
                    "status": "reviewable_no_recognized_command",
                    "detail": "Role has no command requiring this narrow pytest-node check.",
                }
            )
            continue
        for command_index, command_raw in enumerate(commands):
            command = command_raw if isinstance(command_raw, str) else ""
            targets = _python_pytest_node_targets(command)
            if targets is None:
                checks.append(
                    {
                        "role": role,
                        "command_index": command_index,
                        "command": command,
                        "status": "reviewable_unknown_command_form",
                    }
                )
                continue
            for target in targets:
                result = _inspect_python_pytest_target(
                    workspace=workspace,
                    target=target,
                )
                check = {
                    "role": role,
                    "command_index": command_index,
                    "command": command,
                    "recognized_form": "python_-m_pytest_node",
                    "target": target,
                    **result,
                }
                checks.append(check)
                if result.get("status") == "correction_required":
                    failures.append(check)
    receipt = {
        "schema_version": 1,
        "status": "correction_required" if failures else "passed",
        "verified_implementation_head": verified_implementation_head,
        # ``clean_merged_commit_worktree`` already verifies HEAD before yielding.
        "workspace_head": verified_implementation_head,
        "mandatory_roles": list(mandatory_roles),
        "checks": checks,
        "failure_count": len(failures),
        "recorded_at_utc": _utc_now_z(),
    }
    _write_outcome_executability_receipt(receipt_path, receipt)
    if failures:
        raise OutcomeContractNotExecutable(
            receipt_path=receipt_path,
            failures=failures,
        )
    return receipt


def verify_premerge_original_scenario(
    *,
    repo_root: Path,
    owner_root: Path,
    fingerprint: str,
    ticket_markdown: str,
    current: dict[str, Any],
    selected_provenance: dict[str, Any],
    role_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prove the researched behavior on the exact verified PR head before merge.

    This is intentionally a causal gate, not a breadth/scope gate.  It runs only the
    plan-bound original scenario, in a clean detached checkout, so a symptom-only
    patch is caught before external merge mutation while unrelated changes remain a
    semantic-review concern.
    """

    repo_root = repo_root.expanduser().resolve()
    owner_root = owner_root.expanduser().resolve()
    expected_state = expected_outcome_state_from_markdown(ticket_markdown)
    _validate_outcome_role_alignment(
        ticket_markdown=ticket_markdown,
        current=current,
        selected_provenance=selected_provenance,
        expected_state=expected_state,
    )
    ticket_provenance = current.get("ticket_provenance")
    verified_head = (
        ticket_provenance.get("verified_implementation_head")
        if isinstance(ticket_provenance, dict)
        else None
    )
    if not isinstance(verified_head, str) or not verified_head.strip():
        raise ValueError("Pre-merge outcome proof is missing verified implementation head")
    provisional = {**current, "merged_commit": verified_head.strip()}
    runs_root = (repo_root / "runs" / "usertest_implement").resolve()
    output_path = (
        runs_root
        / "_premerge_outcome_roles"
        / fingerprint
        / _timestamp_id()
        / "original_scenario"
        / "outcome_role.json"
    )
    mandatory_roles = _mandatory_premerge_outcome_roles(
        requires_live=_requires_live_from_markdown(ticket_markdown),
        expected_state=expected_state,
    )
    executability_receipt_path = (
        output_path.parent.parent / "outcome_contract_executability.json"
    )
    with clean_merged_commit_worktree(
        repository=owner_root,
        merged_commit=verified_head,
        worktrees_root=_resolve_outcome_worktrees_root(repo_root=repo_root),
        fingerprint=fingerprint,
    ) as workspace:
        _require_mandatory_outcome_commands_executable(
            workspace=workspace,
            selected_provenance=selected_provenance,
            mandatory_roles=mandatory_roles,
            verified_implementation_head=verified_head.strip(),
            receipt_path=executability_receipt_path,
        )
        evidence = _run_role(
            role="original_scenario",
            workspace=workspace,
            output_path=output_path,
            current=provisional,
            selected_provenance=selected_provenance,
            trusted_runs_root=runs_root,
            trusted_oracle_assets_root=_resolve_outcome_trusted_runs_root(repo_root=repo_root),
            role_runner=role_runner or run_outcome_evidence_role,
        )
        return {
            **evidence,
            "outcome_contract_executability_receipt": str(
                executability_receipt_path
            ),
        }


def _restore_test_verified_baseline(
    *,
    current: dict[str, Any],
    ledger_path: Path,
    ticket_path: Path,
    fingerprint: str,
) -> dict[str, Any]:
    if current["state"] in {
        "tests_verified",
        "original_scenario_verified",
        "live_verified",
    }:
        return current
    if not _passing_evidence(current, "test_evidence"):
        raise ValueError(
            "Post-merge outcome cannot progress without retained passing test evidence"
        )
    return _transition(
        ledger_path=ledger_path,
        ticket_path=ticket_path,
        fingerprint=fingerprint,
        state="tests_verified",
        updates={},
    )


def progress_post_merge_outcome(
    *,
    repo_root: Path,
    owner_root: Path,
    ticket_path: Path,
    ledger_path: Path,
    role_runner: Callable[..., dict[str, Any]] | None = None,
) -> OutcomeProgressionResult:
    """Drive one generated merged plan to its researched outcome claim.

    This is deliberately an outcome proof workflow, not another PR-scope gate. It
    replays the original problem on a clean checkout of the exact merge, exercises a
    live boundary only when the plan classified one as material, and distinguishes a
    demonstrated mitigation from a demonstrated resolution.
    """

    repo_root = repo_root.expanduser().resolve()
    owner_root = owner_root.expanduser().resolve()
    ticket_path = ticket_path.expanduser().resolve()
    ledger_path = ledger_path.expanduser().resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=ticket_path,
        fingerprint=None,
    )
    if not is_generated_backlog_ticket(selected.ticket_markdown):
        current = _load_current(ticket_path)
        return OutcomeProgressionResult(
            fingerprint=selected.fingerprint,
            ticket_path=ticket_path,
            starting_state=str(current["state"]),
            final_state=str(current["state"]),
            expected_state=None,
            status="not_applicable_external",
        )
    current = _load_current(ticket_path)
    starting_state = str(current["state"])
    expected_state: str | None = None
    roles_run: list[str] = []
    try:
        selected_provenance = _selected_ticket_provenance(
            selected,
            require_local_plan=True,
        )
        expected_state = expected_outcome_state_from_markdown(selected.ticket_markdown)
        _validate_outcome_role_alignment(
            ticket_markdown=selected.ticket_markdown,
            current=current,
            selected_provenance=selected_provenance,
            expected_state=expected_state,
        )
        if current["state"] == expected_state:
            _require_terminal_outcome_provenance(
                current=current,
                repo_root=repo_root,
                owner_root=owner_root,
            )
            return OutcomeProgressionResult(
                fingerprint=selected.fingerprint,
                ticket_path=ticket_path,
                starting_state=starting_state,
                final_state=str(current["state"]),
                expected_state=expected_state,
                status="complete",
            )
        if current["state"] in _TERMINAL_IMPLEMENTATION_STATES:
            raise ValueError(
                "Durable outcome terminal state disagrees with the researched plan: "
                f"expected={expected_state} observed={current['state']}"
            )
        current = _restore_test_verified_baseline(
            current=current,
            ledger_path=ledger_path,
            ticket_path=ticket_path,
            fingerprint=selected.fingerprint,
        )

        requires_live = current.get("requires_live_verification") is True
        runs_root = (repo_root / "runs" / "usertest_implement").resolve()
        trusted_oracle_assets_root = _resolve_outcome_trusted_runs_root(repo_root=repo_root)
        roles_needed: list[str] = []
        if not _has_bound_passing_role_evidence(
            current=current,
            field="original_scenario_evidence",
            role="original_scenario",
            selected_provenance=selected_provenance,
            trusted_runs_root=runs_root,
        ):
            roles_needed.append("original_scenario")
        # A natural live role is required to claim live verification or resolution. It is not
        # required for a deliberately bounded ``mitigated`` outcome when the separate faithful
        # mitigation-effect role passes. Otherwise an unavailable external precondition would
        # erase demonstrated controlled progress and force permanent zero throughput.
        if expected_state == "resolved" and requires_live and not _has_bound_passing_role_evidence(
            current=current,
            field="live_evidence",
            role="live",
            selected_provenance=selected_provenance,
            trusted_runs_root=runs_root,
        ):
            roles_needed.append("live")
        if expected_state == "mitigated" and not _has_bound_passing_role_evidence(
            current=current,
            field="mitigation_evidence",
            role="mitigation_effect",
            selected_provenance=selected_provenance,
            trusted_runs_root=runs_root,
        ):
            roles_needed.append("mitigation_effect")

        worktrees_root = _resolve_outcome_worktrees_root(repo_root=repo_root)
        runner = role_runner or run_outcome_evidence_role
        if roles_needed:
            _, execution_commit, _ = _verification_execution_provenance(current)
            with clean_merged_commit_worktree(
                repository=owner_root,
                merged_commit=execution_commit,
                worktrees_root=worktrees_root,
                fingerprint=selected.fingerprint,
            ) as workspace:
                for role in roles_needed:
                    current = _load_current(ticket_path)
                    output_path = (
                        runs_root
                        / "_outcome_roles"
                        / selected.fingerprint
                        / _timestamp_id()
                        / role
                        / "outcome_role.json"
                    )
                    evidence = _run_role(
                        role=role,
                        workspace=workspace,
                        output_path=output_path,
                        current=current,
                        selected_provenance=selected_provenance,
                        trusted_runs_root=runs_root,
                        trusted_oracle_assets_root=trusted_oracle_assets_root,
                        role_runner=runner,
                    )
                    roles_run.append(role)
                    if role == "original_scenario":
                        current = _transition(
                            ledger_path=ledger_path,
                            ticket_path=ticket_path,
                            fingerprint=selected.fingerprint,
                            state="original_scenario_verified",
                            updates={
                                "original_scenario_evidence": _merge_evidence(
                                    current, "original_scenario_evidence", evidence
                                ),
                                "remaining_risks": _remaining_risks_after_role(
                                    current, completed_role=role
                                ),
                            },
                        )
                    elif role == "live":
                        current = _transition(
                            ledger_path=ledger_path,
                            ticket_path=ticket_path,
                            fingerprint=selected.fingerprint,
                            state="live_verified",
                            updates={
                                "live_evidence": _merge_evidence(
                                    current, "live_evidence", evidence
                                ),
                                "remaining_risks": _remaining_risks_after_role(
                                    current, completed_role=role
                                ),
                            },
                        )
                    else:
                        current = _transition(
                            ledger_path=ledger_path,
                            ticket_path=ticket_path,
                            fingerprint=selected.fingerprint,
                            state="mitigated",
                            updates={
                                "mitigation_evidence": _merge_evidence(
                                    current, "mitigation_evidence", evidence
                                ),
                                "remaining_risks": [
                                    *_remaining_risks_after_role(
                                        current, completed_role=role
                                    ),
                                    (
                                        "The researched plan demonstrates mitigation; the "
                                        "underlying failure mechanism is not claimed resolved."
                                    ),
                                ],
                            },
                        )

        current = _load_current(ticket_path)
        if expected_state == "mitigated" and current["state"] != "mitigated":
            if not _passing_evidence(current, "original_scenario_evidence"):
                raise ValueError("Original-scenario proof is missing after role execution")
            if not _passing_evidence(current, "mitigation_evidence"):
                raise ValueError("Mitigation-effect proof is missing after role execution")
            mitigation_risk = (
                "The researched plan demonstrates mitigation; the underlying failure "
                "mechanism is not claimed resolved."
            )
            remaining = _remaining_risks_after_role(
                current,
                completed_role="mitigation_effect",
            )
            if mitigation_risk not in remaining:
                remaining.append(mitigation_risk)
            current = _transition(
                ledger_path=ledger_path,
                ticket_path=ticket_path,
                fingerprint=selected.fingerprint,
                state="mitigated",
                updates={"remaining_risks": remaining},
            )
        if expected_state == "resolved" and current["state"] != "resolved":
            if not _passing_evidence(current, "original_scenario_evidence"):
                raise ValueError("Original-scenario proof is missing after role execution")
            if requires_live and not _passing_evidence(current, "live_evidence"):
                raise ValueError("Required live proof is missing after role execution")
            remaining = _remaining_risks_after_role(
                current,
                completed_role="live" if requires_live else "original_scenario",
            )
            if _RECURRENCE_RISK not in remaining:
                remaining.append(_RECURRENCE_RISK)
            current = _transition(
                ledger_path=ledger_path,
                ticket_path=ticket_path,
                fingerprint=selected.fingerprint,
                state="resolved",
                updates={
                    "remaining_risks": remaining,
                    "recurrence_check": {
                        "status": "not_observed",
                        "result": "no_new_source_window",
                        "evidence": [],
                    },
                },
            )
        if current["state"] != expected_state:
            raise ValueError(
                "Post-merge roles passed but did not reach the researched outcome: "
                f"expected={expected_state} observed={current['state']}"
            )
        _require_terminal_outcome_provenance(
            current=current,
            repo_root=repo_root,
            owner_root=owner_root,
        )
        return OutcomeProgressionResult(
            fingerprint=selected.fingerprint,
            ticket_path=ticket_path,
            starting_state=starting_state,
            final_state=str(current["state"]),
            expected_state=expected_state,
            status="complete",
            roles_run=tuple(roles_run),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        blocked = _record_blocked_progression(
            ledger_path=ledger_path,
            ticket_path=ticket_path,
            fingerprint=selected.fingerprint,
            detail=str(exc),
        )
        return OutcomeProgressionResult(
            fingerprint=selected.fingerprint,
            ticket_path=ticket_path,
            starting_state=starting_state,
            final_state=str(blocked["state"]),
            expected_state=expected_state,
            status="blocked",
            roles_run=tuple(roles_run),
            detail=str(exc),
        )


def progress_pending_outcomes_before_refresh(
    *,
    repo_root: Path,
    owner_root: Path,
    ledger_path: Path | None = None,
) -> list[OutcomeProgressionResult]:
    """Retry every generated merged outcome before mining or exporting new work."""

    repo_root = repo_root.expanduser().resolve()
    owner_root = owner_root.expanduser().resolve()
    resolved_ledger = (
        ledger_path.expanduser().resolve()
        if ledger_path is not None
        else (repo_root / ".agents" / "state" / "backlog_implement_actions.yaml").resolve()
    )
    complete_dir = owner_root / ".agents" / "plans" / "5 - complete"
    if not complete_dir.is_dir() or not resolved_ledger.is_file():
        return []
    results: list[OutcomeProgressionResult] = []
    for ticket_path in sorted(complete_dir.glob("*.md"), key=lambda path: path.name):
        try:
            markdown = ticket_path.read_text(encoding="utf-8")
            current = extract_outcome_markdown(markdown)
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            current is None
            or not is_generated_backlog_ticket(markdown)
            or current["state"] not in _PROGRESSIBLE_OUTCOME_STATES
        ):
            continue
        results.append(
            progress_post_merge_outcome(
                repo_root=repo_root,
                owner_root=owner_root,
                ticket_path=ticket_path,
                ledger_path=resolved_ledger,
            )
        )
    return results


__all__ = [
    "OutcomeContractNotExecutable",
    "OutcomeProgressionResult",
    "clean_merged_commit_worktree",
    "expected_outcome_state_from_markdown",
    "progress_pending_outcomes_before_refresh",
    "progress_post_merge_outcome",
    "verify_premerge_original_scenario",
]
