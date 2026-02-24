from __future__ import annotations

import shutil
from pathlib import Path


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


def _try_ignore_workspace_file_in_git(workspace_dir: Path, dest_path: Path) -> None:
    git_dir = _resolve_git_dir(workspace_dir)
    if git_dir is None:
        return

    exclude_path = git_dir / "info" / "exclude"
    try:
        rel = dest_path.relative_to(workspace_dir).as_posix().strip()
    except ValueError:
        return
    if not rel:
        return

    pattern = f"/{rel}"
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


def _materialize_agent_prompt_into_workspace(
    *,
    workspace_dir: Path,
    name: str,
    src_path: Path | None,
    text: str | None,
) -> Path:
    """
    Some missions instruct agents to read prompt append files from the workspace root (agent CWD).

    The runner also stages prompt override/append files under the run directory's `agent_prompts/`
    for artifact stability, but that directory is not the agent working directory. Materialize a
    copy into the acquired workspace so relative reads like `append_system_prompt.md` succeed.
    """

    dest_path = workspace_dir / name
    try:
        if src_path is not None:
            if src_path.resolve() != dest_path.resolve():
                shutil.copyfile(src_path, dest_path)
        else:
            assert text is not None
            dest_path.write_text(text, encoding="utf-8", newline="\n")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Failed to materialize agent prompt file into workspace.\n"
            f"name={name}\n"
            f"workspace_dir={workspace_dir}\n"
            f"src_path={src_path}\n"
            f"error={exc}"
        ) from exc

    _try_ignore_workspace_file_in_git(workspace_dir, dest_path)
    return dest_path
