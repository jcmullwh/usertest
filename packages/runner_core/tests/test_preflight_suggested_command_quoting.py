from __future__ import annotations

from runner_core.runner import _format_usertest_rerun_command


def test_format_usertest_rerun_command_does_not_double_escape_backslashes() -> None:
    repo = r"C:\code\my_repo"
    cmd = _format_usertest_rerun_command(
        ["python", "-m", "usertest.cli", "run", "--repo", repo]
    )

    assert r"C:\\code\\my_repo" not in cmd
    assert r"C:\code\my_repo" in cmd


def test_format_usertest_rerun_command_quotes_whitespace_args() -> None:
    repo = r"C:\code\my repo"
    cmd = _format_usertest_rerun_command(
        ["python", "-m", "usertest.cli", "run", "--repo", repo]
    )

    assert r"C:\code\my repo" in cmd
    assert " --repo " in cmd
