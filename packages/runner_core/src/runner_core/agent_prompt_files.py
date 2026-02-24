from __future__ import annotations

import shutil
from pathlib import Path


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
    for artifact stability, but that directory is not the agent working directory. Materialize a copy
    into the acquired workspace so relative reads like `append_system_prompt.md` succeed.
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

    return dest_path

