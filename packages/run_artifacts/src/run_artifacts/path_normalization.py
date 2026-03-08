from __future__ import annotations

from pathlib import Path


def normalize_agent_path(path_str: str) -> str:
    """
    Produce a canonical agent-visible POSIX path.

    Replaces host-native separators with POSIX ones for use in verification
    environments (e.g. Docker sandboxes) where POSIX expectations are mandatory.
    """
    if not path_str:
        return ""

    # Always use POSIX separators for agent-visible paths.
    normalized = path_str.replace("\\", "/")

    # Special case: if it looks like a Windows path (e.g. C:/...), don't strip
    # redundant slashes yet if they are part of a UNC path or similar,
    # but for usertest agents we mostly care about POSIX mounts.
    while "//" in normalized:
        normalized = normalized.replace("//", "/")

    return normalized


def agent_path_join(root: str, leaf: str) -> str:
    """
    Join two paths with a POSIX separator, ensuring canonical agent-visible form.
    """
    root_norm = normalize_agent_path(root).rstrip("/")
    leaf_norm = normalize_agent_path(leaf).lstrip("/")

    if not root_norm:
        if root.startswith(("/", "\\")):
            return "/" + leaf_norm
        return leaf_norm
    if not leaf_norm:
        return root_norm

    return f"{root_norm}/{leaf_norm}"


def ensure_runs_usertest_exists(workspace_dir: Path) -> Path:
    """
    Ensure `runs/usertest` exists within the workspace to satisfy clean-environment
    assumptions during verification.
    """
    target = workspace_dir / "runs" / "usertest"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Best effort for read-only or permission-blocked environments.
        pass
    return target


def agent_path_for_staged_file(
    staged_path: Path,
    *,
    run_dir: Path,
    run_dir_mount: str | None,
) -> str:
    """
    Translate a host-native staged path into a canonical agent-visible POSIX path.

    Uses `run_dir_mount` if provided, otherwise falls back to a normalized
    absolute path.
    """
    if run_dir_mount is None:
        # Fallback to normalized absolute path when no mount is provided.
        # resolve() is host-native, but we normalize it for the agent.
        try:
            abs_path = str(staged_path.resolve())
        except OSError:
            abs_path = str(staged_path)
        return normalize_agent_path(abs_path)

    mount = run_dir_mount.strip().replace("\\", "/").rstrip("/")
    if not mount:
        mount = "/run_dir"
    if not mount.startswith("/"):
        mount = f"/{mount}"

    try:
        # relative_to needs matching drives on Windows; as_posix() converts to /.
        rel = staged_path.resolve().relative_to(run_dir.resolve()).as_posix()
    except (ValueError, OSError):
        # Fallback if paths are on different drives or cannot be resolved.
        rel = ""

    if not rel:
        return mount

    # rel from as_posix() is already normalized, but we join it canonical-style.
    return agent_path_join(mount, rel)
