from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any


def _git_bytes(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
    )


def _output_bytes(value: bytes | str | None) -> bytes:
    """Normalize subprocess output, including test doubles that return text."""

    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return b""


def capture_runner_implementation_provenance(repo_root: Path) -> dict[str, Any]:
    """Bind a run to the runner implementation, separately from the target revision."""

    root = repo_root.resolve()
    head_result = _git_bytes(root, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        return {
            "schema_version": 1,
            "available": False,
            "repo_root": str(root),
            "reason": "runner_repo_revision_unavailable",
        }

    head = _output_bytes(head_result.stdout).decode("utf-8", errors="replace").strip()
    status_result = _git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    diff_result = _git_bytes(root, "diff", "--binary", "HEAD", "--")
    if status_result.returncode != 0 or diff_result.returncode != 0:
        return {
            "schema_version": 1,
            "available": False,
            "repo_root": str(root),
            "head_commit": head,
            "reason": "runner_repo_state_unavailable",
        }

    status_bytes = _output_bytes(status_result.stdout)
    diff_bytes = _output_bytes(diff_result.stdout)
    untracked: list[dict[str, Any]] = []
    for entry in status_bytes.split(b"\0"):
        if not entry.startswith(b"?? "):
            continue
        relative_text = entry[3:].decode("utf-8", errors="replace")
        candidate = (root / relative_text).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        payload = candidate.read_bytes()
        untracked.append(
            {
                "path": relative_text.replace("\\", "/"),
                "size_bytes": len(payload),
                "sha256": sha256(payload).hexdigest(),
            }
        )
    untracked.sort(key=lambda item: str(item["path"]))
    identity_payload = {
        "head_commit": head,
        "status_sha256": sha256(status_bytes).hexdigest(),
        "tracked_diff_sha256": sha256(diff_bytes).hexdigest(),
        "untracked_files": untracked,
    }
    identity_sha = sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "available": True,
        "repo_root": str(root),
        "head_commit": head,
        "dirty": bool(status_bytes),
        "status_sha256": identity_payload["status_sha256"],
        "tracked_diff_sha256": identity_payload["tracked_diff_sha256"],
        "tracked_diff_size_bytes": len(diff_bytes),
        "untracked_file_count": len(untracked),
        "untracked_files": untracked,
        "implementation_identity_sha256": identity_sha,
    }


__all__ = ("capture_runner_implementation_provenance",)
