from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from runner_core import capture_local_verification

from usertest_implement.implementation_provenance import (
    record_existing_verified_implementation_head,
    validate_verified_implementation_head,
)
from usertest_implement.ledger import update_ledger_file
from usertest_implement.outcome_evidence import (
    validate_bound_runner_verification,
    validate_runner_ticket_ref,
)
from usertest_implement.resume_state import (
    LIFECYCLE_AWAITING_REVIEW,
    LIFECYCLE_CI_FAILED,
    LIFECYCLE_IMPLEMENTED_LOCAL,
    LIFECYCLE_MERGE_READY,
    LIFECYCLE_PUSH_FAILED,
    LIFECYCLE_REVIEW_BLOCKED,
    LIFECYCLE_REVIEW_CHANGES_REQUESTED,
    RESUME_STATE_ARTIFACT_NAME,
    write_ticket_resume_state,
)
from usertest_implement.review_context import _collect_pr_review_context
from usertest_implement.selection import (
    _select_review_ticket,
    _selected_ticket_provenance,
)
from usertest_implement.shared import (
    _resolve_ledger_path,
    _resolve_repo_root,
    _utc_now_z,
    _write_json,
)

_VERIFIED_PRE_MERGE_SOURCE_LIFECYCLES = frozenset(
    {
        LIFECYCLE_IMPLEMENTED_LOCAL,
        LIFECYCLE_PUSH_FAILED,
        LIFECYCLE_CI_FAILED,
        LIFECYCLE_AWAITING_REVIEW,
        LIFECYCLE_REVIEW_CHANGES_REQUESTED,
        LIFECYCLE_REVIEW_BLOCKED,
        LIFECYCLE_MERGE_READY,
    }
)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise ValueError(
            "handoff_adoption_git_failed:"
            + (proc.stderr.strip() or proc.stdout.strip() or " ".join(args))
        )
    return proc.stdout.strip()


def _require_ancestor(
    workspace: Path,
    *,
    ancestor: str,
    descendant: str,
    label: str,
) -> None:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode == 0:
        return
    if proc.returncode == 1:
        raise ValueError(f"handoff_adoption_{label}_not_ancestor")
    raise ValueError(
        f"handoff_adoption_{label}_ancestry_unresolved:"
        + (proc.stderr.strip() or proc.stdout.strip() or ancestor)
    )


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _sha(value: Any, *, label: str) -> str:
    text = _required_text(value, label=label).casefold()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text) is None:
        raise ValueError(f"{label} must be a Git object ID")
    return text


def _repository_identity(url: str) -> str:
    value = url.strip().rstrip("/")
    if re.match(r"^[^/@\s]+@[^:\s]+:.+$", value):
        host_and_path = value.split("@", 1)[1]
        host, path = host_and_path.split(":", 1)
    else:
        parsed = urlparse(value)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    identity = f"{host.casefold()}/{path.casefold().strip('/')}"
    if identity.count("/") < 2 or identity.endswith("/"):
        raise ValueError(f"Unable to identify Git repository from URL: {url!r}")
    return identity


def _pull_request_repository_identity(pr_url: str) -> str:
    parsed = urlparse(pr_url.strip().rstrip("/"))
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or len(parts) != 4
        or parts[2] != "pull"
        or not parts[3].isdigit()
    ):
        raise ValueError(f"Unsupported pull request URL: {pr_url!r}")
    return f"{parsed.hostname.casefold()}/{parts[0].casefold()}/{parts[1].casefold()}"


def _pr_binding(
    context: dict[str, Any],
    *,
    expected_url: str,
    expected_branch: str,
    expected_head: str,
    expected_base_branch: str,
) -> dict[str, Any]:
    raw = context.get("pr")
    if not isinstance(raw, dict):
        raise ValueError("handoff_adoption_pr_context_missing")
    url = _required_text(raw.get("url"), label="PR url").rstrip("/")
    if url != expected_url.rstrip("/"):
        raise ValueError("handoff_adoption_pr_url_mismatch")
    state = _required_text(raw.get("state"), label="PR state").upper()
    if state != "OPEN":
        raise ValueError(f"handoff_adoption_pr_not_open:{state}")
    branch = _required_text(raw.get("headRefName"), label="PR head branch")
    head = _sha(raw.get("headRefOid"), label="PR head SHA")
    base_branch = _required_text(raw.get("baseRefName"), label="PR base branch")
    base_oid = _sha(raw.get("baseRefOid"), label="PR base SHA")
    if branch != expected_branch:
        raise ValueError("handoff_adoption_pr_head_branch_mismatch")
    if head != expected_head.casefold():
        raise ValueError("handoff_adoption_pr_head_mismatch")
    if base_branch != expected_base_branch:
        raise ValueError("handoff_adoption_pr_base_branch_mismatch")
    number = raw.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("handoff_adoption_pr_number_invalid")
    draft = raw.get("isDraft")
    if not isinstance(draft, bool):
        raise ValueError("handoff_adoption_pr_draft_flag_invalid")
    return {
        "number": number,
        "url": url,
        "state": state,
        "is_draft": draft,
        "title": str(raw.get("title") or "").strip() or None,
        "head_branch": branch,
        "head_oid": head,
        "base_branch": base_branch,
        "base_oid": base_oid,
    }


def _require_ticket_unchanged(*, path: Path, expected_bytes: bytes) -> None:
    if not path.is_file():
        raise ValueError("handoff_adoption_ticket_path_changed")
    if path.read_bytes() != expected_bytes:
        raise ValueError("handoff_adoption_ticket_mutated")


def _existing_head_relation(
    *,
    workspace: Path,
    source_head: str,
    adopted_head: str,
    pr_base_oid: str,
) -> dict[str, Any]:
    """Classify the only supported head movement for a no-model handoff adoption.

    An unchanged head may reuse an exact source verification receipt. A changed head must be
    the direct result of merging the PR's current base into that exact source head. The command
    then re-runs the plan's exact verification commands and the ordinary review still evaluates
    the complete new head; this is not a way to adopt arbitrary author changes without review.
    """

    if adopted_head == source_head:
        return {
            "kind": "unchanged",
            "source_head": source_head,
            "adopted_head": adopted_head,
            "pr_base_oid": pr_base_oid,
            "parents": [source_head],
            "verification_reuse_allowed": True,
        }

    parents = [
        value.casefold()
        for value in _git(workspace, "show", "-s", "--format=%P", adopted_head).split()
        if value.strip()
    ]
    if len(parents) < 2 or parents[0] != source_head or pr_base_oid not in parents[1:]:
        raise ValueError("handoff_adoption_descendant_not_current_base_merge")
    return {
        "kind": "current_base_merge",
        "source_head": source_head,
        "adopted_head": adopted_head,
        "pr_base_oid": pr_base_oid,
        "parents": parents,
        "verification_reuse_allowed": False,
    }


def _new_adoption_run_dir(*, runs_root: Path, fingerprint: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = runs_root / "_handoff_adoptions" / fingerprint / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _copy_source_verification_artifacts(
    *, source_run_dir: Path, derived_run_dir: Path, verification: dict[str, Any]
) -> None:
    shutil.copy2(source_run_dir / "verification.json", derived_run_dir / "verification.json")
    relative_raw = verification.get("artifacts_dir")
    if not isinstance(relative_raw, str) or not relative_raw.strip():
        return
    relative = Path(relative_raw)
    source = (source_run_dir / relative).resolve()
    destination = (derived_run_dir / relative).resolve()
    if not source.is_relative_to(source_run_dir) or not destination.is_relative_to(derived_run_dir):
        raise ValueError("handoff_adoption_source_verification_path_invalid")
    if source.is_dir():
        shutil.copytree(source, destination)


def _cmd_handoff_adopt_pr(args: Any) -> int:
    """Adopt an existing PR without a model turn or any remote write."""

    started_monotonic = time.monotonic()
    started_at = _utc_now_z()
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.expanduser().resolve()
    source_run_dir = args.source_run_dir.expanduser().resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=None,
    )
    ticket_path = selected.idea_path
    if ticket_path is None:
        raise SystemExit("PR adoption requires a durable local ticket path.")
    ticket_path = ticket_path.resolve()
    ticket_bytes = ticket_path.read_bytes()
    ticket_sha256 = hashlib.sha256(ticket_bytes).hexdigest()

    try:
        selected_provenance = _selected_ticket_provenance(
            selected,
            require_local_plan=True,
        )
        source_state = _read_json_object(
            source_run_dir / "ticket_resume_state.json",
            label="Source ticket resume state",
        )
        source_lifecycle = source_state.get("lifecycle_state")
        if source_lifecycle not in _VERIFIED_PRE_MERGE_SOURCE_LIFECYCLES:
            raise ValueError(
                f"handoff_adoption_source_lifecycle_not_verified_pre_merge:{source_lifecycle}"
            )
        source_state_run_dir = _required_text(
            source_state.get("run_dir"), label="Source resume run_dir"
        )
        if Path(source_state_run_dir).expanduser().resolve() != source_run_dir:
            raise ValueError("handoff_adoption_source_resume_run_mismatch")
        source_state_ticket = source_state.get("ticket")
        source_state_ticket_path = (
            source_state_ticket.get("path") if isinstance(source_state_ticket, dict) else None
        )
        if (
            not isinstance(source_state_ticket_path, str)
            or Path(source_state_ticket_path).expanduser().resolve() != ticket_path
        ):
            raise ValueError("handoff_adoption_source_ticket_mismatch")

        source_ticket_ref, _ = validate_runner_ticket_ref(
            run_dir=source_run_dir,
            fingerprint=selected.fingerprint,
            case_id=str(selected_provenance["case_id"]),
            plan_revision_id=str(selected_provenance["plan_revision_id"]),
            owner_root=owner_root,
            expected_ticket_provenance=selected_provenance,
            require_test_eligible=True,
        )
        source_implementation = validate_verified_implementation_head(run_dir=source_run_dir)
        source_workspace_ref = _read_json_object(
            source_run_dir / "workspace_ref.json",
            label="Source workspace_ref.json",
        )
        source_target_ref = _read_json_object(
            source_run_dir / "target_ref.json",
            label="Source target_ref.json",
        )
        source_git_ref = _read_json_object(
            source_run_dir / "git_ref.json",
            label="Source git_ref.json",
        )
        source_verification = _read_json_object(
            source_run_dir / "verification.json",
            label="Source verification.json",
        )
        workspace = (
            Path(
                _required_text(
                    source_workspace_ref.get("workspace_dir"),
                    label="Source workspace path",
                )
            )
            .expanduser()
            .resolve()
        )
        branch = _required_text(source_git_ref.get("branch"), label="Source branch")
        source_head = _sha(source_git_ref.get("head_commit"), label="Source head")
        execution_base = _sha(source_target_ref.get("commit_sha"), label="Source execution base")
        target_contract = selected_provenance.get("target_contract")
        if not isinstance(target_contract, dict):
            raise ValueError("handoff_adoption_plan_target_contract_missing")
        planned_revision = _sha(
            target_contract.get("repo_revision"),
            label="Planned repository revision",
        )
        if source_state.get("branch") != branch:
            raise ValueError("handoff_adoption_source_branch_mismatch")
        if (
            str(source_implementation.get("verified_implementation_head") or "").casefold()
            != source_head
        ):
            raise ValueError("handoff_adoption_source_verified_head_mismatch")
        if (
            str(source_implementation.get("execution_base_revision") or "").casefold()
            != execution_base
        ):
            raise ValueError("handoff_adoption_source_execution_base_mismatch")
        head = _sha(_git(workspace, "rev-parse", "HEAD"), label="Workspace head")
        if _git(workspace, "branch", "--show-current") != branch:
            raise ValueError("handoff_adoption_workspace_branch_mismatch")
        if _git(workspace, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("handoff_adoption_workspace_dirty")
        _require_ancestor(
            workspace,
            ancestor=source_head,
            descendant=head,
            label="source_head",
        )
        _require_ancestor(
            workspace,
            ancestor=planned_revision,
            descendant=head,
            label="planned_revision",
        )
        _require_ancestor(
            workspace,
            ancestor=execution_base,
            descendant=head,
            label="execution_base",
        )

        timeout_raw = source_verification.get("timeout_seconds")
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
            raise ValueError("handoff_adoption_source_timeout_missing")
        timeout_seconds = float(timeout_raw)
        if timeout_seconds <= 0:
            raise ValueError("handoff_adoption_source_timeout_not_positive")
        python_executable_raw = _required_text(
            source_verification.get("python_executable"),
            label="Source verification Python executable",
        )
        python_executable = Path(python_executable_raw).expanduser().resolve()
        if not python_executable.is_file():
            raise ValueError("handoff_adoption_source_python_executable_missing")

        remote_name = _required_text(args.remote_name, label="Remote name")
        remote_url = _git(workspace, "remote", "get-url", remote_name)
        pr_url = _required_text(args.pr_url, label="PR URL").rstrip("/")
        if _repository_identity(remote_url) != _pull_request_repository_identity(pr_url):
            raise ValueError("handoff_adoption_remote_repository_mismatch")
        base_branch = _required_text(args.base_branch, label="Base branch")
        pre_context = _collect_pr_review_context(
            workspace_dir=workspace,
            pr_url=pr_url,
        )
        pre_binding = _pr_binding(
            pre_context,
            expected_url=pr_url,
            expected_branch=branch,
            expected_head=head,
            expected_base_branch=base_branch,
        )
        _require_ancestor(
            workspace,
            ancestor=planned_revision,
            descendant=str(pre_binding["base_oid"]),
            label="planned_revision_to_pr_base",
        )
        head_relation = _existing_head_relation(
            workspace=workspace,
            source_head=source_head,
            adopted_head=head,
            pr_base_oid=str(pre_binding["base_oid"]),
        )

        source_exact = bool(head_relation["verification_reuse_allowed"])
        source_validation_error: str | None = None
        try:
            validate_bound_runner_verification(
                run_dir=source_run_dir,
                fingerprint=selected.fingerprint,
                case_id=str(selected_provenance["case_id"]),
                plan_revision_id=str(selected_provenance["plan_revision_id"]),
                evidence_kind="test",
                owner_root=owner_root,
                expected_ticket_provenance=selected_provenance,
            )
        except ValueError as exc:
            source_exact = False
            source_validation_error = str(exc)

        runs_root = (
            args.runs_dir.expanduser().resolve()
            if args.runs_dir is not None
            else (repo_root / "runs" / "usertest_implement").resolve()
        )
        derived_run_dir = _new_adoption_run_dir(
            runs_root=runs_root,
            fingerprint=selected.fingerprint,
        )
        workspace_ref = {
            "schema_version": 1,
            "workspace_dir": str(workspace),
            "workspace_strategy": "existing_clean_head",
            "will_cleanup_workspace": False,
            "derived_from_run_dir": str(source_run_dir),
        }
        target_ref = {
            "schema_version": 1,
            "repo_input": str(workspace),
            "ref": branch,
            "commit_sha": execution_base,
            "acquire_mode": "existing_handoff_adoption",
            "model_invoked": False,
            "derived_from_run_dir": str(source_run_dir),
        }
        git_ref = {
            "schema_version": 1,
            "branch": branch,
            "commit_attempted": False,
            "commit_performed": False,
            "commit_observed": True,
            "base_commit": head,
            "head_commit": head,
            "error": None,
        }
        derived_ticket_ref = dict(source_ticket_ref)
        derived_ticket_ref.pop("implementation_provenance", None)
        derived_ticket_ref["derived_from_run_dir"] = str(source_run_dir)
        derived_ticket_ref["model_invoked"] = False
        _write_json(derived_run_dir / "workspace_ref.json", workspace_ref)
        _write_json(derived_run_dir / "target_ref.json", target_ref)
        _write_json(derived_run_dir / "git_ref.json", git_ref)
        _write_json(derived_run_dir / "ticket_ref.json", derived_ticket_ref)

        verification_binding = source_ticket_ref.get("verification_binding")
        if not isinstance(verification_binding, dict):
            raise ValueError("handoff_adoption_verification_binding_missing")
        plan_commands_raw = verification_binding.get("plan_commands")
        if not isinstance(plan_commands_raw, list):
            raise ValueError("handoff_adoption_plan_commands_missing")
        plan_commands = list(plan_commands_raw)
        capture_started_at = _utc_now_z()
        if source_exact:
            _copy_source_verification_artifacts(
                source_run_dir=source_run_dir,
                derived_run_dir=derived_run_dir,
                verification=source_verification,
            )
            verification = _read_json_object(
                derived_run_dir / "verification.json",
                label="Derived verification.json",
            )
            capture_performed = False
        else:
            verification = capture_local_verification(
                run_dir=derived_run_dir,
                cwd=workspace,
                commands=plan_commands,
                timeout_seconds=timeout_seconds,
                python_executable=str(python_executable),
            )
            capture_performed = True
        capture_finished_at = _utc_now_z()

        post_context = _collect_pr_review_context(
            workspace_dir=workspace,
            pr_url=pr_url,
        )
        post_binding = _pr_binding(
            post_context,
            expected_url=pr_url,
            expected_branch=branch,
            expected_head=head,
            expected_base_branch=base_branch,
        )
        if post_binding != pre_binding:
            raise ValueError("handoff_adoption_pr_binding_changed_during_verification")
        if _git(workspace, "rev-parse", "HEAD").casefold() != head:
            raise ValueError("handoff_adoption_head_changed_during_verification")
        if _git(workspace, "branch", "--show-current") != branch:
            raise ValueError("handoff_adoption_branch_changed_during_verification")
        if _git(workspace, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("handoff_adoption_workspace_dirtied_by_verification")
        _require_ticket_unchanged(path=ticket_path, expected_bytes=ticket_bytes)

        capture_ref = {
            "schema_version": 1,
            "kind": "exact_plan_command_verification_capture",
            "source_run_dir": str(source_run_dir),
            "source_verification_sha256": _sha256_file(source_run_dir / "verification.json"),
            "source_exact_plan_receipt": source_exact,
            "source_validation_error": source_validation_error,
            "source_lifecycle_state": source_lifecycle,
            "head_relation": head_relation,
            "capture_performed": capture_performed,
            "model_invoked": False,
            "workspace_mirror_written": False,
            "commands": plan_commands,
            "timeout_seconds": timeout_seconds,
            "python_executable": verification.get("python_executable"),
            "head_before": head,
            "head_after": _git(workspace, "rev-parse", "HEAD").casefold(),
            "started_at_utc": capture_started_at,
            "finished_at_utc": capture_finished_at,
            "verification_sha256": _sha256_file(derived_run_dir / "verification.json"),
        }
        _write_json(
            derived_run_dir / "verification_capture_ref.json",
            capture_ref,
        )
        if verification.get("passed") is not True:
            raise ValueError(
                "handoff_adoption_exact_plan_verification_failed:"
                f"{derived_run_dir / 'verification.json'}"
            )

        implementation_provenance = record_existing_verified_implementation_head(
            run_dir=derived_run_dir
        )
        verification_receipt = validate_bound_runner_verification(
            run_dir=derived_run_dir,
            fingerprint=selected.fingerprint,
            case_id=str(selected_provenance["case_id"]),
            plan_revision_id=str(selected_provenance["plan_revision_id"]),
            evidence_kind="test",
            owner_root=owner_root,
            trusted_runs_root=runs_root,
            expected_ticket_provenance=selected_provenance,
        )
        _write_json(
            derived_run_dir / "verification_receipt.json",
            verification_receipt,
        )

        flags = {
            "adopted": True,
            "commit_observed": True,
            "push_observed": True,
            "pr_adopted": True,
            "commit_performed_by_command": False,
            "push_performed_by_command": False,
            "pr_created_by_command": False,
            "model_invoked": False,
            "ticket_mutated": False,
        }
        adoption_ref = {
            "schema_version": 1,
            "kind": "existing_pr_adoption",
            "adopted_at_utc": _utc_now_z(),
            "source_run_dir": str(source_run_dir),
            "run_dir": str(derived_run_dir),
            "fingerprint": selected.fingerprint,
            "case_id": selected_provenance["case_id"],
            "plan_revision_id": selected_provenance["plan_revision_id"],
            "ticket_path": str(ticket_path),
            "ticket_sha256": ticket_sha256,
            "workspace": str(workspace),
            "branch": branch,
            "head_commit": head,
            "source_head_commit": source_head,
            "source_lifecycle_state": source_lifecycle,
            "head_relation": head_relation,
            "planned_revision": planned_revision,
            "execution_base_revision": execution_base,
            "remote_name": remote_name,
            "remote_url": remote_url,
            "repository_identity": _repository_identity(remote_url),
            "pre_pr_binding": pre_binding,
            "pre_pr_binding_sha256": _sha256_json(pre_binding),
            "post_pr_binding": post_binding,
            "post_pr_binding_sha256": _sha256_json(post_binding),
            "verification_capture_ref_sha256": _sha256_file(
                derived_run_dir / "verification_capture_ref.json"
            ),
            "verification_receipt_sha256": _sha256_file(
                derived_run_dir / "verification_receipt.json"
            ),
            "implementation_provenance": implementation_provenance,
            "flags": flags,
        }
        _write_json(derived_run_dir / "adoption_ref.json", adoption_ref)
        pr_ref = {
            "schema_version": 1,
            "requested": False,
            "created": False,
            "existing_pr": True,
            "adopted": True,
            "pr_adopted": True,
            "created_by_command": False,
            "url": pr_url,
            "number": pre_binding["number"],
            "title": pre_binding["title"],
            "state": pre_binding["state"],
            "draft": pre_binding["is_draft"],
            "branch": branch,
            "head_sha": head,
            "base_branch": base_branch,
            "base_sha": pre_binding["base_oid"],
            "remote_name": remote_name,
            "remote_url": remote_url,
            "error": None,
        }
        _write_json(derived_run_dir / "pr_ref.json", pr_ref)
        handoff_summary = {
            "schema_version": 1,
            "handoff_mode": "adopt_existing_pr",
            "final_status": "success",
            "branch": branch,
            "head_commit": head,
            "commit_requested": False,
            "commit_performed": False,
            "commit_observed": True,
            "push_requested": False,
            "pushed": False,
            "push_observed": True,
            "pr_requested": False,
            "pr_created": False,
            "pr_adopted": True,
            "pr_url": pr_url,
            "review_required": True,
            "review_run_dir": None,
            "verification_passed": True,
            "source_run_dir": str(source_run_dir),
            "adoption_ref": str(derived_run_dir / "adoption_ref.json"),
            "flags": flags,
        }
        _write_json(derived_run_dir / "handoff_summary.json", handoff_summary)
        write_ticket_resume_state(
            selected=selected,
            run_dir=derived_run_dir,
            owner_root=owner_root,
            branch=branch,
            exit_code=0,
            ticket_path_override=ticket_path,
        )

        _require_ticket_unchanged(path=ticket_path, expected_bytes=ticket_bytes)
        ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
        finished_at = _utc_now_z()
        update_ledger_file(
            ledger_path,
            fingerprint=selected.fingerprint,
            updates={
                "title": selected.title,
                "owner_root": str(owner_root),
                "idea_path": str(ticket_path),
                "last_run_dir": str(derived_run_dir),
                "last_exit_code": 0,
                "last_started_at": started_at,
                "last_finished_at": finished_at,
                "last_duration_seconds": max(0.0, time.monotonic() - started_monotonic),
                "last_branch": branch,
                "last_head_commit": head,
                "last_push_remote": remote_name,
                "last_push_remote_url": remote_url,
                "last_pr_url": pr_url,
                "last_handoff_mode": "adopt_existing_pr",
                "last_resume_state_path": str(derived_run_dir / RESUME_STATE_ARTIFACT_NAME),
                "last_resume_lifecycle_state": LIFECYCLE_AWAITING_REVIEW,
                "commit_observed": True,
                "push_observed": True,
                "pr_adopted": True,
            },
        )
        _require_ticket_unchanged(path=ticket_path, expected_bytes=ticket_bytes)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(
        json.dumps(
            {
                "fingerprint": selected.fingerprint,
                "run_dir": str(derived_run_dir),
                "source_run_dir": str(source_run_dir),
                "branch": branch,
                "head_commit": head,
                "source_head_commit": source_head,
                "source_lifecycle_state": source_lifecycle,
                "head_relation": head_relation,
                "pr_url": pr_url,
                "source_verification_reused": source_exact,
                "exact_plan_verification_captured": capture_performed,
                "commands": plan_commands,
                "timeout_seconds": timeout_seconds,
                "model_invoked": False,
                "ticket_mutated": False,
                "ledger_path": str(ledger_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


__all__ = ["_cmd_handoff_adopt_pr"]
