from __future__ import annotations

import json
from pathlib import Path

from agent_adapters.failure_artifacts import write_tool_failure_artifacts


def test_write_tool_failure_artifacts_handles_none_tool_name(tmp_path: Path) -> None:
    out = write_tool_failure_artifacts(
        run_dir=tmp_path,
        failure_index=1,
        tool_name=None,
        tool_input={"x": 1},
        error_text="boom",
    )

    assert isinstance(out.get("dir"), str)
    assert out["dir"].endswith("tool_failures/tool_01_unknown")

    tool_json_rel = out.get("tool_json")
    assert isinstance(tool_json_rel, str) and tool_json_rel
    tool_json_path = tmp_path / tool_json_rel
    assert tool_json_path.exists()

    payload = json.loads(tool_json_path.read_text(encoding="utf-8"))
    assert payload.get("tool") == "unknown"

