from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from usertest_implement.git_ops import (
    DEFAULT_GIT_USER_EMAIL,
    DEFAULT_GIT_USER_NAME,
    checkout_branch,
    commit_all,
    ensure_git_identity,
    ensure_remote,
    head_sha,
    push_branch,
    status_porcelain,
)


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _workspace_dir_from_run_dir(run_dir: Path) -> Path | None:
    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    if not isinstance(workspace_ref, dict):
        return None
    raw = workspace_ref.get("workspace_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw)


def finalize_commit(
    *,
    run_dir: Path,
    branch: str,
    commit_message: str,
    git_user_name: str | None = None,
    git_user_email: str | None = None,
) -> dict[str, Any]:
    git_ref: dict[str, Any] = {
        "schema_version": 1,
        "branch": branch,
        "commit_attempted": True,
        "commit_performed": False,
        "head_commit": None,
        "base_commit": None,
        "error": None,
    }

    target_ref = _read_json(run_dir / "target_ref.json")
    if isinstance(target_ref, dict):
        base = target_ref.get("commit_sha")
        if isinstance(base, str) and base.strip():
            git_ref["base_commit"] = base.strip()

    workspace_dir = _workspace_dir_from_run_dir(run_dir)
    if workspace_dir is None:
        git_ref["error"] = "Missing workspace_ref.json; cannot locate workspace"
        _write_json(run_dir / "git_ref.json", git_ref)
        return git_ref

    try:
        user_name = git_user_name if git_user_name is not None else DEFAULT_GIT_USER_NAME
        user_email = git_user_email if git_user_email is not None else DEFAULT_GIT_USER_EMAIL
        ensure_git_identity(workspace_dir, user_name=user_name, user_email=user_email)
        checkout_branch(workspace_dir, branch)
        if git_ref.get("base_commit") is None:
            git_ref["base_commit"] = head_sha(workspace_dir)
        if status_porcelain(workspace_dir).strip():
            head = commit_all(workspace_dir, message=commit_message)
            git_ref["commit_performed"] = True
            git_ref["head_commit"] = head
        else:
            git_ref["commit_performed"] = False
            git_ref["head_commit"] = head_sha(workspace_dir)
    except Exception as e:  # noqa: BLE001
        git_ref["error"] = str(e)

    _write_json(run_dir / "git_ref.json", git_ref)
    return git_ref


def _coerce_remote_url(*, repo_dir: Path, remote_name: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "get-url", remote_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out if out else None


def finalize_push(
    *,
    run_dir: Path,
    remote_name: str,
    remote_url: str | None,
    candidate_repo_dirs: list[Path],
    branch: str,
    force_with_lease: bool,
) -> dict[str, Any]:
    push_ref: dict[str, Any] = {
        "schema_version": 1,
        "remote_name": remote_name,
        "remote_url": remote_url,
        "branch": branch,
        "force_with_lease": bool(force_with_lease),
        "pushed": False,
        "stdout": None,
        "stderr": None,
        "error": None,
    }

    workspace_dir = _workspace_dir_from_run_dir(run_dir)
    if workspace_dir is None:
        push_ref["error"] = "Missing workspace_ref.json; cannot locate workspace"
        _write_json(run_dir / "push_ref.json", push_ref)
        return push_ref

    resolved_remote_url = remote_url
    if resolved_remote_url is None:
        for candidate in candidate_repo_dirs:
            url = _coerce_remote_url(repo_dir=candidate, remote_name=remote_name)
            if url is not None:
                resolved_remote_url = url
                break

    if resolved_remote_url is None:
        push_ref["error"] = "Unable to determine remote URL; provide --remote-url"
        _write_json(run_dir / "push_ref.json", push_ref)
        return push_ref

    try:
        ensure_remote(workspace_dir, remote_name=remote_name, remote_url=resolved_remote_url)
        result = push_branch(
            workspace_dir,
            remote_name=remote_name,
            branch=branch,
            force_with_lease=bool(force_with_lease),
        )
        push_ref["remote_url"] = resolved_remote_url
        push_ref["pushed"] = True
        push_ref["stdout"] = result.stdout
        push_ref["stderr"] = result.stderr
    except Exception as e:  # noqa: BLE001
        push_ref["remote_url"] = resolved_remote_url
        push_ref["error"] = str(e)

    _write_json(run_dir / "push_ref.json", push_ref)
    return push_ref
