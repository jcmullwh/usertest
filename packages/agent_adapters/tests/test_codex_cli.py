from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_adapters.codex_cli import (
    _resolve_executable,
    run_codex_exec,
    validate_codex_personality_config_overrides,
    validate_codex_reasoning_effort_config_overrides,
)


def _make_refresh_token_reused_dummy_codex(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_refresh_token_reused.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                "import time",
                "",
                "",
                "def main() -> None:",
                "    try:",
                "        sys.stdin.read()",
                "    except Exception:",
                "        pass",
                "",
                "    while True:",
                "        sys.stderr.write(",
                "            'ERROR codex_core::auth: Failed to refresh token: 401 Unauthorized: '",
                "            '{\"error\": {\"code\": \"refresh_token_reused\"}}\\n'",
                "        )",
                "        sys.stderr.flush()",
                "        time.sleep(0.05)",
                "",
                "",
                "if __name__ == '__main__':",
                "    main()",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )

    if os.name == "nt":
        wrapper = tmp_path / "dummy_codex_refresh_token_reused.cmd"
        wrapper.write_text(
            f"@echo off\r\n\"{sys.executable}\" \"{script}\" %*\r\n",
            encoding="utf-8",
            newline="\n",
        )
        return str(wrapper)

    wrapper = tmp_path / "dummy_codex_refresh_token_reused.sh"
    wrapper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f"\"{sys.executable}\" \"{script}\" \"$@\"",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PATH resolution for .cmd files")
def test_resolve_executable_finds_cmd_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = tmp_path / "dummy.cmd"
    cmd.write_text("@echo off\necho dummy_ok\n", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PATHEXT", f"{os.environ.get('PATHEXT', '')};.CMD")

    resolved = _resolve_executable("dummy")
    assert Path(resolved).resolve() == cmd.resolve()

    proc = subprocess.run([resolved], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "dummy_ok" in proc.stdout


def test_run_codex_exec_fails_fast_on_refresh_token_reused(tmp_path: Path) -> None:
    dummy_binary = _make_refresh_token_reused_dummy_codex(tmp_path)

    stderr_path = tmp_path / "stderr.txt"
    raw_events_path = tmp_path / "raw_events.jsonl"
    last_message_path = tmp_path / "last_message.txt"

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="test",
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        sandbox="read-only",
        ask_for_approval="never",
        binary=dummy_binary,
        timeout_seconds=1.0,
    )

    assert result.exit_code != 0
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "Codex authentication failed: refresh_token_reused" in stderr_text
    assert "codex logout" in stderr_text
    assert "codex login" in stderr_text


def test_validate_codex_personality_config_overrides_requires_model_messages() -> None:
    for key in ("model_personality", "personality"):
        issue = validate_codex_personality_config_overrides(
            [
                f'{key}="pragmatic"',
                "model_reasoning_effort=high",
            ]
        )

        assert issue is not None
        assert "model_messages is missing" in issue.message
        assert "model_messages" in issue.hint
        assert issue.details.get("personality_keys") == [key]
        assert issue.details.get("model_messages_keys") == []


def test_validate_codex_personality_config_overrides_accepts_matching_model_messages() -> None:
    issue = validate_codex_personality_config_overrides(
        [
            'personality="pragmatic"',
            'model_messages=[{"role":"system","content":"Be concise."}]',
        ]
    )

    assert issue is None


def test_validate_codex_reasoning_effort_config_overrides_rejects_invalid_value() -> None:
    issue = validate_codex_reasoning_effort_config_overrides(
        [
            "model_reasoning_effort=xhigh",
            "profile.model_reasoning_effort='xhigh'",
        ]
    )

    assert issue is not None
    assert "invalid" in issue.message.lower()
    assert "xhigh" in issue.message
    assert "model_reasoning_effort=high" in issue.hint
    details = issue.details
    assert details.get("allowed_values") == ["minimal", "low", "medium", "high"]
    invalid_entries = details.get("invalid_entries")
    assert isinstance(invalid_entries, list)
    assert len(invalid_entries) == 2


def test_validate_codex_reasoning_effort_config_overrides_accepts_supported_value() -> None:
    issue = validate_codex_reasoning_effort_config_overrides(
        [
            "model_reasoning_effort=high",
        ]
    )

    assert issue is None
