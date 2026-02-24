from __future__ import annotations

from pathlib import Path

import pytest

import runner_core.runner as runner


@pytest.mark.parametrize("command", ["rejected", "'rejected'", '"rejected"'])
def test_run_verification_commands_does_not_execute_rejection_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    calls: list[object] = []

    def _unexpected_run(*args: object, **kwargs: object) -> object:  # pragma: no cover
        calls.append((args, kwargs))
        raise AssertionError("subprocess.run should not be called for rejection sentinels")

    monkeypatch.setattr(runner.subprocess, "run", _unexpected_run)

    summary = runner._run_verification_commands(
        run_dir=tmp_path,
        attempt_number=1,
        commands=[command],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
    )

    assert calls == []
    assert summary.get("passed") is False
    commands = summary.get("commands")
    assert isinstance(commands, list)
    assert len(commands) == 1
    result = commands[0]
    assert isinstance(result, dict)
    assert result.get("command") == command.strip()
    assert result.get("exit_code") == 126
    assert result.get("rejected_sentinel") is True

    stderr_path = tmp_path / "verification" / "attempt1" / "cmd_01.stderr.txt"
    assert stderr_path.exists()
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "rejection sentinel" in stderr_text.lower()

