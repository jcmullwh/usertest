# Regression tests for BLG-012: Windows command generation drops backslashes.
#
# These tests verify that Windows absolute paths (e.g., C:\\Users\\...\\python.exe) are
# preserved exactly through the command parsing/formatting layer used by codex_normalize.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_adapters import normalize_codex_events
from agent_adapters.codex_normalize import (
    _format_argv,
    _maybe_unwrap_shell_command,
    _split_command,
)
from agent_adapters.events import iter_events_jsonl

# ---------------------------------------------------------------------------
# _split_command: backslash preservation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,expected_first_token",
    [
        # Standard python.exe paths
        (
            r"C:\Python313\python.exe -m pytest --version",
            r"C:\Python313\python.exe",
        ),
        (
            r"C:\Users\jason\AppData\Local\Programs\Python\Python313\python.exe -m pytest -q",
            r"C:\Users\jason\AppData\Local\Programs\Python\Python313\python.exe",
        ),
        # Path with spaces
        (
            r'"C:\Program Files\Python313\python.exe" -m pytest --version',
            r"C:\Program Files\Python313\python.exe",
        ),
        # py.exe launcher
        (
            r"C:\Users\jason\AppData\Local\Programs\Python\Launcher\py.exe -m pytest",
            r"C:\Users\jason\AppData\Local\Programs\Python\Launcher\py.exe",
        ),
        # pytest.exe
        (
            r"C:\Python313\Scripts\pytest.exe -q",
            r"C:\Python313\Scripts\pytest.exe",
        ),
        # WindowsApps/Packages location
        (
            r"C:\Users\j\AppData\Local\Packages\PSF.Python.3.13\Scripts\pytest.exe -q",
            r"C:\Users\j\AppData\Local\Packages\PSF.Python.3.13\Scripts\pytest.exe",
        ),
        # venv Scripts path
        (
            r"I:\code\usertest\apps\usertest_implement\.venv\Scripts\python.exe -m pytest",
            r"I:\code\usertest\apps\usertest_implement\.venv\Scripts\python.exe",
        ),
    ],
)
def test_split_command_preserves_windows_backslashes(
    command: str, expected_first_token: str
) -> None:
    """_split_command must not drop backslashes from Windows absolute paths."""
    tokens = _split_command(command)
    assert tokens, f"Expected non-empty token list for: {command!r}"
    first = tokens[0]
    # Strip surrounding quotes that posix=False may retain
    first_stripped = first.strip("\"'")
    assert "\\" in first_stripped or first_stripped == expected_first_token, (
        f"Backslash lost in first token.\n"
        f"Input:    {command!r}\n"
        f"Got:      {first!r}\n"
        f"Expected: {expected_first_token!r}"
    )
    assert first_stripped == expected_first_token, (
        f"First token mismatch.\n"
        f"Input:    {command!r}\n"
        f"Got:      {first_stripped!r}\n"
        f"Expected: {expected_first_token!r}"
    )


def test_split_command_posix_paths_still_work() -> None:
    """Non-Windows paths (no backslashes) must still be split correctly."""
    tokens = _split_command("/usr/bin/python3 -m pytest -q")
    assert tokens == ["/usr/bin/python3", "-m", "pytest", "-q"]


# ---------------------------------------------------------------------------
# _maybe_unwrap_shell_command: Windows path in powershell -Command
# ---------------------------------------------------------------------------


def test_maybe_unwrap_shell_command_preserves_windows_path_in_powershell() -> None:
    """
    When unwrapping powershell -Command "& 'C:\\...\\python.exe' -m pytest",
    the inner command must not lose backslashes.
    """
    argv = [
        "powershell",
        "-Command",
        r"& 'C:\Python313\python.exe' -m pytest --version",
    ]
    result = _maybe_unwrap_shell_command(argv)
    joined = " ".join(result)
    # The Windows path must survive unwrapping
    assert r"C:\Python313\python.exe" in joined or r"C:\Python313\python.exe" in "".join(
        t.strip("\"'") for t in result
    ), (
        f"Windows path backslashes lost after unwrapping.\n"
        f"Input argv: {argv!r}\n"
        f"Got tokens: {result!r}"
    )


def test_maybe_unwrap_shell_command_preserves_windows_path_in_cmd() -> None:
    """
    When unwrapping cmd /c "C:\\...\\python.exe -m pytest", backslashes must survive.
    """
    argv = [
        "cmd",
        "/c",
        r"C:\Python313\python.exe -m pytest --version",
    ]
    result = _maybe_unwrap_shell_command(argv)
    joined = " ".join(t.strip("\"'") for t in result)
    assert r"C:\Python313\python.exe" in joined, (
        f"Windows path backslashes lost after cmd unwrapping.\n"
        f"Input argv: {argv!r}\n"
        f"Got tokens: {result!r}"
    )


# ---------------------------------------------------------------------------
# _format_argv: round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [r"C:\Python313\python.exe", "-m", "pytest", "--version"],
        [r"C:\Users\jason\AppData\Local\Programs\Python\Launcher\py.exe", "-m", "pytest"],
        [r"C:\Program Files\Python313\python.exe", "-m", "pytest", "-q"],
        [r"C:\Users\j\AppData\Local\Packages\PSF.Python.3.13\Scripts\pytest.exe", "-q"],
    ],
)
def test_format_argv_preserves_windows_backslashes(argv: list[str]) -> None:
    """_format_argv must not drop backslashes from Windows path tokens."""
    formatted = _format_argv(argv)
    # The drive+colon+backslash pattern must survive
    for token in argv:
        if "\\" in token:
            # The token should appear literally or quoted in the output
            assert token in formatted or token.replace("\\", "\\\\") in formatted, (
                f"Windows path token not preserved in formatted command.\n"
                f"Token:     {token!r}\n"
                f"Formatted: {formatted!r}"
            )


# ---------------------------------------------------------------------------
# End-to-end: normalize_codex_events with Windows command_execution items
# ---------------------------------------------------------------------------


def test_normalize_codex_events_preserves_windows_path_in_command_execution(
    tmp_path: Path,
) -> None:
    """
    When a Codex command_execution item contains a Windows absolute path,
    the normalized run_command event must preserve the backslashes in its
    'command' field.

    Regression for BLG-012: command strings like
    'C:\\Python313\\python.exe -m pytest' were being normalized to
    'C:Python313python.exe -m pytest' due to POSIX shlex splitting.
    """
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    python_path = r"C:\Python313\python.exe"
    cmd_string = f"{python_path} -m pytest --version"

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "1",
                        "msg": {"type": "agent_message", "message": "Running pytest"},
                    }
                ),
                json.dumps(
                    {
                        "id": "2",
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": cmd_string,
                            "exit_code": 1,
                            "output": "ModuleNotFoundError: No module named 'pytest'",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    run_cmds = [e for e in events if e.get("type") == "run_command"]

    assert run_cmds, "Expected at least one run_command event"
    cmd_event = run_cmds[0]
    recorded_command = cmd_event.get("data", {}).get("command", "")

    # The Windows path must NOT be corrupted (e.g. C:Python313python.exe)
    assert "C:" in recorded_command, (
        f"Drive letter lost. recorded_command={recorded_command!r}"
    )
    assert "\\" in recorded_command, (
        f"Backslash(es) lost from Windows path in normalized command.\n"
        f"Input:  {cmd_string!r}\n"
        f"Got:    {recorded_command!r}\n"
        f"Expected backslash to appear in: {recorded_command!r}"
    )
    # The path must not collapse into drive-relative form like C:Python313...
    assert "C:P" not in recorded_command and "C:U" not in recorded_command, (
        f"Windows path collapsed to drive-relative form (missing backslash after drive letter).\n"
        f"Input:  {cmd_string!r}\n"
        f"Got:    {recorded_command!r}"
    )


def test_normalize_codex_events_preserves_windows_path_with_spaces(
    tmp_path: Path,
) -> None:
    """
    Windows paths with spaces (e.g., C:\\Program Files\\...) must survive normalization.
    """
    (tmp_path / "USERS.md").write_text("# Users\n", encoding="utf-8")

    python_path = r"C:\Program Files\Python313\python.exe"
    cmd_string = f'"{python_path}" -m pytest --version'

    raw = tmp_path / "raw.jsonl"
    raw.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "1",
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": cmd_string,
                            "exit_code": 1,
                            "output": "error",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    normalized = tmp_path / "normalized.jsonl"
    normalize_codex_events(
        raw_events_path=raw,
        normalized_events_path=normalized,
        workspace_root=tmp_path,
    )

    events = list(iter_events_jsonl(normalized))
    run_cmds = [e for e in events if e.get("type") == "run_command"]
    assert run_cmds, "Expected at least one run_command event"
    recorded_command = run_cmds[0].get("data", {}).get("command", "")

    assert "\\" in recorded_command, (
        f"Backslash(es) lost from Windows path with spaces.\n"
        f"Input:  {cmd_string!r}\n"
        f"Got:    {recorded_command!r}"
    )
