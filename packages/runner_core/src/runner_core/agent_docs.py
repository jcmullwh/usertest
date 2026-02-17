from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def obfuscate_target_agent_docs(*, workspace_dir: Path, run_dir: Path) -> dict[str, Any]:
    """
    Hide target-repo agent instruction docs from the workspace root.

    The immediate goal is to prevent agents from automatically discovering and treating
    the target repo's agent-facing instruction files (such as `agents.md`) as binding
    rules for the run.

    Current scope (intentionally narrow):
    - Only root-level `agents.md` and `AGENTS.md` are obfuscated.

    Behavior:
    - Copies originals into the run artifacts directory for auditability.
    - Moves the originals out of the workspace root into a hidden directory under
      `.usertest_hidden/agent_docs/`.
    - Writes a JSON manifest at `<run_dir>/obfuscated_agent_docs.json`.
    """

    workspace_dir = workspace_dir.resolve()
    run_dir = run_dir.resolve()

    candidates = [
        workspace_dir / "agents.md",
        workspace_dir / "AGENTS.md",
    ]

    hidden_dir = workspace_dir / ".usertest_hidden" / "agent_docs"
    hidden_dir.mkdir(parents=True, exist_ok=True)

    artifacts_root = run_dir / "obfuscated_agent_docs"
    artifacts_original = artifacts_root / "original"
    artifacts_original.mkdir(parents=True, exist_ok=True)

    moved: list[dict[str, str]] = []
    skipped: list[str] = []

    for src in candidates:
        if not src.exists():
            skipped.append(src.name)
            continue
        if src.is_dir():
            raise RuntimeError(f"Refusing to obfuscate directory named {src.name!r} at repo root.")

        artifact_copy = artifacts_original / src.name
        shutil.copy2(src, artifact_copy)

        dest = _unique_dest_path(hidden_dir / src.name)
        try:
            src.replace(dest)
        except OSError:
            shutil.move(str(src), str(dest))

        moved.append(
            {
                "original_relpath": src.relative_to(workspace_dir).as_posix(),
                "workspace_hidden_relpath": dest.relative_to(workspace_dir).as_posix(),
                "artifact_original_copy_relpath": artifact_copy.relative_to(run_dir).as_posix(),
            }
        )

    manifest: dict[str, Any] = {
        "workspace_root": str(workspace_dir),
        "artifacts_root": str(run_dir),
        "candidates": [p.name for p in candidates],
        "moved": moved,
        "skipped": skipped,
    }

    manifest_path = run_dir / "obfuscated_agent_docs.json"
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    return manifest


def _unique_dest_path(path: Path) -> Path:
    """
    Return a non-existing path, suffixing with `.N` when needed.
    """

    if not path.exists():
        return path

    parent = path.parent
    stem = path.name
    for i in range(1, 1000):
        candidate = parent / f"{stem}.{i}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a unique destination path for {path}")
