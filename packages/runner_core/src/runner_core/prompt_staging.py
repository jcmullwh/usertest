from __future__ import annotations

import shutil
from pathlib import Path

from runner_core.pathing import LOCAL_BACKEND_RUN_DIR_ALIAS, agent_path_join, normalize_agent_path


def _resolve_agent_prompt_input_path(*, raw: Path, repo_root: Path, workspace_dir: Path) -> Path:
    if raw.is_absolute():
        candidate = raw
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
        raise FileNotFoundError(f"Agent prompt file not found: {raw}")

    candidates = [
        workspace_dir / raw,
        repo_root / raw,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    raise FileNotFoundError(
        f"Agent prompt file not found.\ninput={raw}\ntried={', '.join(str(p) for p in candidates)}"
    )


def _stage_agent_prompt_text(*, run_dir: Path, name: str, text: str) -> Path:
    dest_dir = run_dir / "agent_prompts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name
    dest_path.write_text(text, encoding="utf-8")
    return dest_path


def _stage_agent_prompt_file(*, run_dir: Path, name: str, src_path: Path) -> Path:
    dest_dir = run_dir / "agent_prompts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name
    shutil.copyfile(src_path, dest_path)
    return dest_path


def _agent_path_for_staged_file(
    staged_path: Path, *, run_dir: Path, run_dir_mount: str | None
) -> str:
    if run_dir_mount is None:
        return str(staged_path.resolve())

    mount = normalize_agent_path(run_dir_mount)
    if not mount or mount == ".":
        mount = "/run_dir"
    if not mount.startswith("/"):
        mount = f"/{mount}"

    rel = staged_path.resolve().relative_to(run_dir.resolve()).as_posix()
    return agent_path_join(mount, rel)


def _run_dir_agent_visible_root(
    *, run_dir: Path, run_dir_mount: str | None, workspace_dir: Path
) -> Path:
    """
    Resolve the canonical physical root for run_dir-scoped content that an agent's own
    exec/read tools must reach at runtime: the verification broker's client script and its
    per-attempt request/response files.

    Docker backend bind-mounts `run_dir` into the sandbox alongside the workspace, so
    physical storage stays under `run_dir` (already reachable there). Local backend has no
    such mount: an agent confined to its own workspace -- and any subprocess it spawns, such
    as the broker client script -- cannot reach `run_dir` at all, so physical storage must
    live inside `workspace_dir` instead, under an alias excluded from workspace state
    hashing (see `workspace_state_hash.py`) so this runner-owned scratch content never
    affects agent-change detection.
    """
    if run_dir_mount is not None:
        return run_dir
    return workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS


__all__ = (
    "_agent_path_for_staged_file",
    "_resolve_agent_prompt_input_path",
    "_run_dir_agent_visible_root",
    "_stage_agent_prompt_file",
    "_stage_agent_prompt_text",
)
