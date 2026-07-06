from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from agent_adapters import probe_agent_shell_launch


def _make_marker_agent(tmp_path: Path) -> str:
    script = tmp_path / "marker_agent.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import sys",
                "sys.stdin.read()",
                "print('shell_probe=ok')",
                "last_path = None",
                "args = sys.argv[1:]",
                "for idx, arg in enumerate(args):",
                "    if arg == '--output-last-message' and idx + 1 < len(args):",
                "        last_path = args[idx + 1]",
                "if last_path:",
                "    with open(last_path, 'w', encoding='utf-8', newline='\\n') as f:",
                "        f.write('shell_probe=ok\\n')",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    if os.name == "nt":
        wrapper = tmp_path / "marker_agent.cmd"
        wrapper.write_text(
            f"@echo off\r\n\"{sys.executable}\" \"{script}\" %*\r\n",
            encoding="utf-8",
            newline="\n",
        )
        return str(wrapper)
    wrapper = tmp_path / "marker_agent.sh"
    wrapper.write_text(
        "#!/bin/sh\n" f"exec \"{sys.executable}\" \"{script}\" \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_probe_agent_shell_launch_uses_codex_adapter_path(tmp_path: Path) -> None:
    binary = _make_marker_agent(tmp_path)

    result = probe_agent_shell_launch(
        agent="codex",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "probe",
        binary=binary,
        codex_sandbox="workspace-write",
        codex_ask_for_approval="never",
    )

    payload = result.to_dict()
    assert payload["kind"] == "agent_shell_payload"
    assert payload["agent"] == "codex"
    assert payload["ok"] is True
    assert payload["marker_seen"] is True
    assert "exec" in result.argv
    assert "--sandbox" in result.argv
    assert "workspace-write" in result.argv


def test_probe_agent_shell_launch_uses_claude_and_gemini_adapter_paths(tmp_path: Path) -> None:
    binary = _make_marker_agent(tmp_path)

    claude = probe_agent_shell_launch(
        agent="claude",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "claude_probe",
        binary=binary,
        claude_allowed_tools=["Bash"],
    ).to_dict()
    assert claude["ok"] is True
    assert claude["agent"] == "claude"

    gemini = probe_agent_shell_launch(
        agent="gemini",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "gemini_probe",
        binary=binary,
        gemini_allowed_tools=["run_shell_command"],
        gemini_sandbox=False,
    ).to_dict()
    assert gemini["ok"] is True
    assert gemini["agent"] == "gemini"


def test_probe_agent_shell_launch_failure_is_structured(tmp_path: Path) -> None:
    result = probe_agent_shell_launch(
        agent="codex",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "probe",
        binary=str(tmp_path / "missing-codex"),
        codex_sandbox="read-only",
    ).to_dict()

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert result["reason"]
