from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backlog_repo import extract_outcome_markdown, verify_outcome_record_provenance
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

    proc = subprocess.run(
        ["git", "-C", str(repository), *args],
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
        try:
            revalidated = validate_bound_outcome_role_receipt(
                role_artifact_path=Path(artifact_path),
                evidence_kind=role,
                case_id=str(current["case_id"]),
                plan_revision_id=str(current["plan_revision_id"]),
                merged_commit=str(current["merged_commit"]),
                expected_ticket_provenance=bound,
                trusted_runs_root=trusted_runs_root,
                expected_role_artifact_sha256=artifact_sha256,
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
    verification = verify_outcome_record_provenance(
        current,
        trusted_runs_roots=[(repo_root / "runs").resolve()],
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
        if (
            risk.startswith("Post-merge outcome verification is blocked:")
            and completed_role in risk
        ):
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
    runner_kwargs: dict[str, Any] = {}
    if isinstance(role_contract.get("oracle"), dict):
        runner_kwargs["trusted_oracle_assets_root"] = trusted_oracle_assets_root
    artifact = role_runner(
        workspace=workspace,
        output_path=output_path,
        role=role,
        role_contract=role_contract,
        case_id=str(current["case_id"]),
        plan_revision_id=str(current["plan_revision_id"]),
        merged_commit=str(current["merged_commit"]),
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
        merged_commit=str(current["merged_commit"]),
        expected_ticket_provenance=bound,
        trusted_runs_root=trusted_runs_root,
    )
    return {
        "kind": "runner_outcome_role",
        "reference": str(output_path),
        "result": "passed",
        "outcome_oracle_id": receipt.get("outcome_oracle_id"),
        "proof_scope": receipt.get("proof_scope"),
        "runner_receipt": receipt,
    }


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
    with clean_merged_commit_worktree(
        repository=owner_root,
        merged_commit=verified_head,
        worktrees_root=repo_root / ".tmp" / "outcome_worktrees",
        fingerprint=fingerprint,
    ) as workspace:
        return _run_role(
            role="original_scenario",
            workspace=workspace,
            output_path=output_path,
            current=provisional,
            selected_provenance=selected_provenance,
            trusted_runs_root=runs_root,
            trusted_oracle_assets_root=(repo_root / "runs").resolve(),
            role_runner=role_runner or run_outcome_evidence_role,
        )


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
        roles_needed: list[str] = []
        if not _has_bound_passing_role_evidence(
            current=current,
            field="original_scenario_evidence",
            role="original_scenario",
            selected_provenance=selected_provenance,
            trusted_runs_root=runs_root,
        ):
            roles_needed.append("original_scenario")
        if requires_live and not _has_bound_passing_role_evidence(
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

        worktrees_root = repo_root / ".tmp" / "outcome_worktrees"
        runner = role_runner or run_outcome_evidence_role
        if roles_needed:
            with clean_merged_commit_worktree(
                repository=owner_root,
                merged_commit=str(current["merged_commit"]),
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
                        trusted_oracle_assets_root=(repo_root / "runs").resolve(),
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
            if requires_live and not _passing_evidence(current, "live_evidence"):
                raise ValueError("Required live proof is missing after role execution")
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
    "OutcomeProgressionResult",
    "clean_merged_commit_worktree",
    "expected_outcome_state_from_markdown",
    "progress_pending_outcomes_before_refresh",
    "progress_post_merge_outcome",
    "verify_premerge_original_scenario",
]
