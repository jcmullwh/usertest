from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from runner_core.pathing import agent_path_join, normalize_agent_path


@dataclass(frozen=True)
class AgentVisiblePath:
    host_path: Path
    agent_path: str
    relative_path: str


def agent_visible_run_subpath(
    *,
    run_dir: Path,
    subpath: Path,
    run_dir_mount: str | None,
    workspace_dir: Path | None = None,
) -> AgentVisiblePath:
    """
    Resolve a run-dir-relative path onto the canonical agent-visible contract.

    - When the execution backend exposes `run_dir` inside the agent runtime, return the mounted
      `/run_dir/...` path.
    - Otherwise, use a workspace mirror so the surfaced path stays agent-readable from the
      workspace root. In that case the returned `agent_path` stays relative to the workspace
      root on purpose (for example `verification/...`).
    """

    rel = Path(subpath)
    rel_posix = normalize_agent_path(rel.as_posix())

    if run_dir_mount is not None:
        mount = normalize_agent_path(run_dir_mount).rstrip("/")
        if not mount or mount == ".":
            mount = "/run_dir"
        if not mount.startswith("/"):
            mount = f"/{mount}"
        return AgentVisiblePath(
            host_path=run_dir / rel,
            agent_path=agent_path_join(mount, rel_posix),
            relative_path=rel_posix,
        )

    if workspace_dir is not None:
        return AgentVisiblePath(
            host_path=workspace_dir / rel,
            agent_path=rel_posix,
            relative_path=rel_posix,
        )

    host_path = run_dir / rel
    try:
        agent_path = normalize_agent_path(str(host_path.resolve()))
    except OSError:
        agent_path = normalize_agent_path(str(host_path))
    return AgentVisiblePath(
        host_path=host_path,
        agent_path=agent_path,
        relative_path=rel_posix,
    )


def mirror_path_into_workspace(
    *,
    source_path: Path,
    dest_path: Path,
    workspace_dir: Path,
) -> None:
    """
    Copy a file or directory into the workspace mirror used by agent-visible paths.
    """

    if source_path == dest_path:
        return

    _try_ignore_workspace_path_in_git(workspace_dir=workspace_dir, dest_path=dest_path)

    if source_path.is_dir():
        if dest_path.exists():
            if dest_path.is_dir():
                shutil.rmtree(dest_path)
            else:
                dest_path.unlink()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, dest_path)
        return

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_path)


def ensure_workspace_dir(
    *,
    path: Path,
    workspace_dir: Path,
) -> None:
    _try_ignore_workspace_path_in_git(workspace_dir=workspace_dir, dest_path=path)
    path.mkdir(parents=True, exist_ok=True)


def _resolve_git_dir(workspace_dir: Path) -> Path | None:
    dot_git = workspace_dir / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    try:
        payload = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not payload.lower().startswith("gitdir:"):
        return None
    raw = payload[len("gitdir:") :].strip()
    if not raw:
        return None
    git_dir = Path(raw)
    if git_dir.is_absolute():
        return git_dir
    return (workspace_dir / git_dir).resolve()


def _try_ignore_workspace_path_in_git(*, workspace_dir: Path, dest_path: Path) -> None:
    git_dir = _resolve_git_dir(workspace_dir)
    if git_dir is None:
        return

    try:
        rel = dest_path.relative_to(workspace_dir).as_posix().strip()
    except ValueError:
        return
    if not rel:
        return

    pattern = f"/{rel}"
    exclude_path = git_dir / "info" / "exclude"
    try:
        existing = ""
        if exclude_path.exists():
            existing = exclude_path.read_text(encoding="utf-8", errors="replace")
        existing_lines = {line.strip() for line in existing.splitlines() if line.strip()}
        if pattern in existing_lines:
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = "" if (not existing or existing.endswith("\n")) else "\n"
        exclude_path.write_text(
            existing + suffix + pattern + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError:
        return
