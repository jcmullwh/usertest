from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _git_diff(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "diff"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout


def _git_numstat(path: Path) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["git", "-C", str(path), "diff", "--numstat"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, removed_s, file_path = parts
        try:
            added = int(added_s) if added_s != "-" else 0
            removed = int(removed_s) if removed_s != "-" else 0
        except ValueError:
            continue
        out.append({"path": file_path, "lines_added": added, "lines_removed": removed})
    return out


def _git_status_porcelain(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout


def _ensure_git_user_config(path: Path) -> None:
    email = subprocess.run(
        ["git", "-C", str(path), "config", "user.email"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout.strip()
    name = subprocess.run(
        ["git", "-C", str(path), "config", "user.name"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout.strip()

    if not email:
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "usertest@local"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    if not name:
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "usertest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )


def _maybe_commit_preprocess_workspace(path: Path, *, message: str) -> str | None:
    status = _git_status_porcelain(path)
    if not status.strip():
        return None

    _ensure_git_user_config(path)
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()


__all__ = (
    "_ensure_git_user_config",
    "_git_diff",
    "_git_numstat",
    "_git_status_porcelain",
    "_maybe_commit_preprocess_workspace",
)
