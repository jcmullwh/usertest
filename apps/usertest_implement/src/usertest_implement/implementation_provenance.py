from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backlog_repo.plan_scope import validate_plan_target_contract


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"implementation_provenance_artifact_invalid:{path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"implementation_provenance_artifact_invalid:{path.name}")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise ValueError(
            "implementation_provenance_git_failed:"
            + (proc.stderr.strip() or proc.stdout.strip() or args[0])
        )
    return proc.stdout.strip()


def _git_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ValueError(
        "implementation_provenance_git_failed:"
        + (proc.stderr.strip() or proc.stdout.strip() or "merge-base --is-ancestor")
    )


def _contract_from_ticket_ref(ticket_ref: Mapping[str, Any]) -> dict[str, Any]:
    raw = ticket_ref.get("ticket_provenance")
    provenance = raw if isinstance(raw, Mapping) else {}
    return validate_plan_target_contract(provenance.get("target_contract"))


def record_verified_implementation_head(
    *,
    run_dir: Path,
    require_exact_base: bool,
) -> dict[str, Any]:
    """Bind passing verification to the exact commit subsequently reviewed."""

    ticket_ref_path = run_dir / "ticket_ref.json"
    ticket_ref = _read_json(ticket_ref_path)
    contract = _contract_from_ticket_ref(ticket_ref)
    verification_path = run_dir / "verification.json"
    target_ref_path = run_dir / "target_ref.json"
    git_ref_path = run_dir / "git_ref.json"
    verification = _read_json(verification_path)
    target_ref = _read_json(target_ref_path)
    git_ref = _read_json(git_ref_path)
    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    if verification.get("passed") is not True:
        raise ValueError("implementation_provenance_verification_not_passed")
    if git_ref.get("commit_performed") is not True:
        raise ValueError("implementation_provenance_commit_not_performed")
    head = str(git_ref.get("head_commit") or "").strip().casefold()
    execution_base = str(target_ref.get("commit_sha") or "").strip().casefold()
    git_base = str(git_ref.get("base_commit") or "").strip().casefold()
    planned = str(contract["repo_revision"]).casefold()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None:
        raise ValueError("implementation_provenance_head_invalid")
    if not execution_base or git_base != execution_base:
        raise ValueError("implementation_provenance_execution_base_mismatch")
    if require_exact_base and execution_base != planned:
        raise ValueError("implementation_provenance_target_revision_mismatch")
    workspace_raw = workspace_ref.get("workspace_dir")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        raise ValueError("implementation_provenance_workspace_missing")
    workspace = Path(workspace_raw).expanduser().resolve()
    if _git(workspace, "rev-parse", "HEAD").casefold() != head:
        raise ValueError("implementation_provenance_workspace_head_mismatch")
    if _git(workspace, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("implementation_provenance_workspace_dirty_after_commit")
    payload = {
        "schema_version": 1,
        "repo_revision": contract["repo_revision"],
        "execution_base_revision": execution_base,
        "verified_implementation_head": head,
        "verification_sha256": _sha256_file(verification_path),
        "target_ref_sha256": _sha256_file(target_ref_path),
        "git_ref_sha256": _sha256_file(git_ref_path),
    }
    receipt = {
        **payload,
        "receipt_sha256": hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
    }
    ticket_ref["implementation_provenance"] = receipt
    ticket_ref_path.write_text(
        json.dumps(ticket_ref, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return receipt


def record_existing_verified_implementation_head(*, run_dir: Path) -> dict[str, Any]:
    """Bind a passing capture to an already committed, unchanged clean head.

    Unlike :func:`record_verified_implementation_head`, this contract never
    represents a commit performed by the current command.  It is intentionally
    limited to a clean named branch whose current HEAD equals the recorded
    pre-existing head, with both the researched revision and the implementation
    execution base in that head's ancestry. Exact stage-6 command coverage is a
    separate receipt validated by the handoff/outcome workflow.
    """

    ticket_ref_path = run_dir / "ticket_ref.json"
    ticket_ref = _read_json(ticket_ref_path)
    contract = _contract_from_ticket_ref(ticket_ref)
    verification_path = run_dir / "verification.json"
    target_ref_path = run_dir / "target_ref.json"
    git_ref_path = run_dir / "git_ref.json"
    workspace_ref_path = run_dir / "workspace_ref.json"
    verification = _read_json(verification_path)
    target_ref = _read_json(target_ref_path)
    git_ref = _read_json(git_ref_path)
    workspace_ref = _read_json(workspace_ref_path)
    if verification.get("passed") is not True:
        raise ValueError("implementation_provenance_verification_not_passed")
    if not isinstance(git_ref.get("commit_attempted"), bool):
        raise ValueError("implementation_provenance_existing_head_commit_attempt_invalid")
    if git_ref.get("commit_performed") is not False:
        raise ValueError("implementation_provenance_existing_head_commit_performed")
    if git_ref.get("commit_observed") is not True:
        raise ValueError("implementation_provenance_existing_head_commit_unobserved")

    head = str(git_ref.get("head_commit") or "").strip().casefold()
    git_base = str(git_ref.get("base_commit") or "").strip().casefold()
    execution_base = str(target_ref.get("commit_sha") or "").strip().casefold()
    planned = str(contract["repo_revision"]).strip().casefold()
    branch = str(git_ref.get("branch") or "").strip()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None:
        raise ValueError("implementation_provenance_head_invalid")
    if git_base != head:
        raise ValueError("implementation_provenance_existing_head_base_mismatch")
    if not execution_base:
        raise ValueError("implementation_provenance_execution_base_mismatch")
    if not branch:
        raise ValueError("implementation_provenance_existing_head_branch_missing")
    workspace_raw = workspace_ref.get("workspace_dir")
    if not isinstance(workspace_raw, str) or not workspace_raw.strip():
        raise ValueError("implementation_provenance_workspace_missing")
    workspace = Path(workspace_raw).expanduser().resolve()
    if _git(workspace, "rev-parse", "HEAD").casefold() != head:
        raise ValueError("implementation_provenance_workspace_head_mismatch")
    if _git(workspace, "branch", "--show-current") != branch:
        raise ValueError("implementation_provenance_workspace_branch_mismatch")
    if _git(workspace, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("implementation_provenance_workspace_dirty_existing_head")
    if not _git_is_ancestor(workspace, planned, head):
        raise ValueError("implementation_provenance_planned_revision_not_ancestor")
    if not _git_is_ancestor(workspace, execution_base, head):
        raise ValueError("implementation_provenance_execution_base_not_ancestor")

    payload = {
        "schema_version": 2,
        "provenance_mode": "existing_clean_head",
        "repo_revision": contract["repo_revision"],
        "execution_base_revision": execution_base,
        "verified_implementation_head": head,
        "verification_sha256": _sha256_file(verification_path),
        "target_ref_sha256": _sha256_file(target_ref_path),
        "git_ref_sha256": _sha256_file(git_ref_path),
        "workspace_ref_sha256": _sha256_file(workspace_ref_path),
    }
    receipt = {
        **payload,
        "receipt_sha256": hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest(),
    }
    ticket_ref["implementation_provenance"] = receipt
    ticket_ref_path.write_text(
        json.dumps(ticket_ref, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return receipt


def validate_verified_implementation_head(*, run_dir: Path) -> dict[str, Any]:
    """Revalidate the run artifacts that bind tests to the reviewed commit."""

    ticket_ref = _read_json(run_dir / "ticket_ref.json")
    contract = _contract_from_ticket_ref(ticket_ref)
    raw = ticket_ref.get("implementation_provenance")
    if not isinstance(raw, Mapping) or raw.get("schema_version") not in {1, 2}:
        raise ValueError("implementation_provenance_receipt_missing")
    if raw.get("schema_version") == 1:
        required = {
            "schema_version",
            "repo_revision",
            "execution_base_revision",
            "verified_implementation_head",
            "verification_sha256",
            "target_ref_sha256",
            "git_ref_sha256",
            "receipt_sha256",
        }
    else:
        required = {
            "schema_version",
            "provenance_mode",
            "repo_revision",
            "execution_base_revision",
            "verified_implementation_head",
            "verification_sha256",
            "target_ref_sha256",
            "git_ref_sha256",
            "workspace_ref_sha256",
            "receipt_sha256",
        }
    if set(raw) != required:
        raise ValueError("implementation_provenance_receipt_fields_invalid")
    payload = {key: raw[key] for key in raw if key != "receipt_sha256"}
    expected_receipt = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if raw.get("receipt_sha256") != expected_receipt:
        raise ValueError("implementation_provenance_receipt_hash_mismatch")
    if raw.get("repo_revision") != contract.get("repo_revision"):
        raise ValueError("implementation_provenance_contract_revision_mismatch")
    for field, filename in (
        ("verification_sha256", "verification.json"),
        ("target_ref_sha256", "target_ref.json"),
        ("git_ref_sha256", "git_ref.json"),
    ):
        if raw.get(field) != _sha256_file(run_dir / filename):
            raise ValueError(f"implementation_provenance_{field}_mismatch")
    if raw.get("schema_version") == 2:
        if raw.get("provenance_mode") != "existing_clean_head":
            raise ValueError("implementation_provenance_mode_invalid")
        if raw.get("workspace_ref_sha256") != _sha256_file(
            run_dir / "workspace_ref.json"
        ):
            raise ValueError("implementation_provenance_workspace_ref_sha256_mismatch")
    verification = _read_json(run_dir / "verification.json")
    target_ref = _read_json(run_dir / "target_ref.json")
    git_ref = _read_json(run_dir / "git_ref.json")
    if verification.get("passed") is not True:
        raise ValueError("implementation_provenance_verification_not_passed")
    if str(target_ref.get("commit_sha") or "").casefold() != str(
        raw["execution_base_revision"]
    ).casefold():
        raise ValueError("implementation_provenance_execution_base_mismatch")
    if str(git_ref.get("head_commit") or "").casefold() != str(
        raw["verified_implementation_head"]
    ).casefold():
        raise ValueError("implementation_provenance_git_head_mismatch")
    if raw.get("schema_version") == 2:
        if not isinstance(git_ref.get("commit_attempted"), bool):
            raise ValueError("implementation_provenance_existing_head_commit_attempt_invalid")
        if git_ref.get("commit_performed") is not False:
            raise ValueError("implementation_provenance_existing_head_commit_performed")
        if git_ref.get("commit_observed") is not True:
            raise ValueError("implementation_provenance_existing_head_commit_unobserved")
        head = str(raw["verified_implementation_head"]).casefold()
        if str(git_ref.get("base_commit") or "").casefold() != head:
            raise ValueError("implementation_provenance_existing_head_base_mismatch")
        workspace_ref = _read_json(run_dir / "workspace_ref.json")
        workspace_raw = workspace_ref.get("workspace_dir")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            raise ValueError("implementation_provenance_workspace_missing")
        workspace = Path(workspace_raw).expanduser().resolve()
        if _git(workspace, "rev-parse", "HEAD").casefold() != head:
            raise ValueError("implementation_provenance_workspace_head_mismatch")
        if _git(workspace, "branch", "--show-current") != str(
            git_ref.get("branch") or ""
        ).strip():
            raise ValueError("implementation_provenance_workspace_branch_mismatch")
        if _git(workspace, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("implementation_provenance_workspace_dirty_existing_head")
        if not _git_is_ancestor(workspace, str(raw["repo_revision"]), head):
            raise ValueError("implementation_provenance_planned_revision_not_ancestor")
        if not _git_is_ancestor(
            workspace,
            str(raw["execution_base_revision"]),
            head,
        ):
            raise ValueError("implementation_provenance_execution_base_not_ancestor")
    return json.loads(_canonical_json(raw))


__all__ = [
    "record_existing_verified_implementation_head",
    "record_verified_implementation_head",
    "validate_verified_implementation_head",
]
