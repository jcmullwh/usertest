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
                "import json",
                "import sys",
                "sys.stdin.read()",
                "codex_event = {",
                "    'msg': {",
                "        'type': 'exec_command_end',",
                "        'stdout': 'shell_probe=ok',",
                "        'stderr': '',",
                "        'exit_code': 0,",
                "    }",
                "}",
                "claude_event = {",
                "    'type': 'user',",
                "    'message': {",
                "        'role': 'user',",
                "        'content': [",
                "            {",
                "                'type': 'tool_result',",
                "                'tool_use_id': 'tool_1',",
                "                'content': 'shell_probe=ok',",
                "                'is_error': False,",
                "            }",
                "        ],",
                "    },",
                "}",
                "gemini_event = {",
                "    'type': 'tool_result',",
                "    'tool_id': 't1',",
                "    'status': 'success',",
                "    'output': 'shell_probe=ok',",
                "}",
                "print(json.dumps(codex_event))",
                "print(json.dumps(claude_event))",
                "print(json.dumps(gemini_event))",
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
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
        return str(wrapper)
    wrapper = tmp_path / "marker_agent.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
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
        codex_ignore_user_config=False,
        codex_ignore_rules=False,
    )

    payload = result.to_dict()
    assert payload["kind"] == "agent_shell_payload"
    assert payload["agent"] == "codex"
    assert payload["ok"] is True
    assert payload["marker_seen"] is True
    assert payload["marker_source"] == "codex.exec_command_end"
    assert "exec" in result.argv
    assert "--sandbox" in result.argv
    assert "workspace-write" in result.argv
    assert "--ignore-user-config" not in result.argv
    assert "--ignore-rules" not in result.argv


def test_probe_agent_shell_launch_accepts_codex_command_execution_aggregated_output(
    tmp_path: Path,
) -> None:
    script = tmp_path / "codex_aggregated_output.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import sys",
                "sys.stdin.read()",
                "event = {",
                "    'item': {",
                "        'type': 'command_execution',",
                "        'command': '/bin/bash -lc \"printf shell_probe=ok\"',",
                "        'aggregated_output': 'shell_probe=ok\\n',",
                "        'exit_code': 0,",
                "        'status': 'completed',",
                "    }",
                "}",
                "print(json.dumps(event))",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    if os.name == "nt":
        binary = tmp_path / "codex_aggregated_output.cmd"
        binary.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
    else:
        binary = tmp_path / "codex_aggregated_output.sh"
        binary.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    result = probe_agent_shell_launch(
        agent="codex",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "probe",
        binary=str(binary),
        codex_sandbox="workspace-write",
        codex_ask_for_approval="never",
    ).to_dict()

    assert result["ok"] is True
    assert result["marker_seen"] is True
    assert result["marker_source"] == "codex.command_execution"


def test_probe_agent_shell_launch_requires_positive_controlled_commands(tmp_path: Path) -> None:
    script = tmp_path / "codex_required_commands.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import sys",
                "sys.stdin.read()",
                "commands = [",
                "    ('git rev-parse --is-inside-work-tree', 'true\\n'),",
                "    ('python --version', 'Python 3.14\\n'),",
                "    (\"Write-Output 'shell_probe=ok'\", 'shell_probe=ok\\n'),",
                "]",
                "for command, output in commands:",
                "    print(json.dumps({'item': {'type': 'command_execution', "
                "'command': 'powershell -Command ' + command, 'aggregated_output': output, "
                "'exit_code': 0, 'status': 'completed'}}))",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    if os.name == "nt":
        binary = tmp_path / "codex_required_commands.cmd"
        binary.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
    else:
        binary = tmp_path / "codex_required_commands.sh"
        binary.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    payload = probe_agent_shell_launch(
        agent="codex",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "probe",
        binary=str(binary),
        codex_sandbox="workspace-write",
        codex_required_commands=[
            "git rev-parse --is-inside-work-tree",
            "python --version",
        ],
        codex_required_command_outputs={
            "git rev-parse --is-inside-work-tree": "true",
            "python --version": "Python ",
        },
    ).to_dict()

    assert payload["ok"] is True
    assert payload["required_commands_seen"] == [
        "git rev-parse --is-inside-work-tree",
        "python --version",
    ]

    wrong_output = probe_agent_shell_launch(
        agent="codex",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "wrong-output-probe",
        binary=str(binary),
        codex_required_commands=["python --version"],
        codex_required_command_outputs={"python --version": "Ruby "},
    ).to_dict()
    assert wrong_output["ok"] is False
    assert wrong_output["required_commands_seen"] == []


def test_probe_agent_shell_launch_fails_when_controlled_command_is_missing(
    tmp_path: Path,
) -> None:
    binary = _make_marker_agent(tmp_path)

    payload = probe_agent_shell_launch(
        agent="codex",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "probe",
        binary=binary,
        codex_required_commands=["python --version"],
    ).to_dict()

    assert payload["ok"] is False
    assert payload["required_commands_seen"] == []
    assert "python --version" in str(payload["reason"])


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
    assert claude["marker_source"] == "claude.tool_result"

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
    assert gemini["marker_source"] == "gemini.tool_result"


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


def test_probe_agent_shell_launch_does_not_accept_final_message_marker(tmp_path: Path) -> None:
    script = tmp_path / "final_only.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import sys",
                "sys.stdin.read()",
                "event = {'msg': {'type': 'agent_message', 'message': 'shell_probe=ok'}}",
                "print(json.dumps(event))",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    if os.name == "nt":
        binary = tmp_path / "final_only.cmd"
        binary.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
    else:
        binary = tmp_path / "final_only.sh"
        binary.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    result = probe_agent_shell_launch(
        agent="codex",
        workspace_dir=tmp_path,
        artifacts_dir=tmp_path / "probe",
        binary=str(binary),
        codex_sandbox="read-only",
    ).to_dict()

    assert result["exit_code"] == 0
    assert result["marker_seen"] is False
    assert result["marker_source"] is None
    assert result["ok"] is False
