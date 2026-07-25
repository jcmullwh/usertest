from __future__ import annotations

from pathlib import Path

import pytest

from usertest.commands.shared import _from_source_import_remediation

_WINDOWS_OFFLINE_FIRST_SUCCESS_COMMAND = (
    "powershell -NoProfile -ExecutionPolicy Bypass -File "
    "./scripts/offline_first_success.ps1"
)
_VULNERABLE_WINDOWS_SCRIPT_PATH = r".\scripts\offline_first_success.ps1"
_WINDOWS_COMMAND_LOCATIONS = (
    "README.md",
    "docs/tutorials/getting-started.md",
    "apps/usertest/src/usertest/commands/shared.py",
    "apps/usertest/src/usertest/first_run_launcher.py",
    "apps/usertest_backlog/src/usertest_backlog/shared.py",
    "apps/usertest_implement/src/usertest_implement/shared.py",
)


def test_from_source_import_remediation_mentions_supported_fixes() -> None:
    msg = _from_source_import_remediation(missing_module="agent_adapters")
    assert "requirements-dev.txt" in msg
    assert _WINDOWS_OFFLINE_FIRST_SUCCESS_COMMAND in msg
    assert "scripts/offline_first_success.sh" in msg
    assert "scripts\\set_pythonpath.ps1" in msg
    assert "scripts/set_pythonpath.sh" in msg


@pytest.mark.parametrize("relative_path", _WINDOWS_COMMAND_LOCATIONS)
def test_windows_offline_first_command_is_cross_shell_safe(relative_path: str) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / relative_path).read_text(encoding="utf-8")

    assert _WINDOWS_OFFLINE_FIRST_SUCCESS_COMMAND in text
    assert _VULNERABLE_WINDOWS_SCRIPT_PATH not in text
