from __future__ import annotations

from runner_core.runner import _infer_shell_policy_status


def test_gemini_shell_allowed_when_outer_sandbox_present() -> None:
    status, reason, allowed_tools = _infer_shell_policy_status(
        agent="gemini",
        claude_policy={},
        gemini_policy={"allowed_tools": ["read_file", "run_shell_command"], "sandbox": True},
        has_outer_sandbox=True,
    )

    assert status == "allowed"
    assert "run_shell_command" in str(reason)
    assert isinstance(allowed_tools, list)
    assert "run_shell_command" in allowed_tools


def test_gemini_shell_blocked_when_no_outer_sandbox_and_policy_disables_sandbox() -> None:
    status, reason, allowed_tools = _infer_shell_policy_status(
        agent="gemini",
        claude_policy={},
        gemini_policy={"allowed_tools": ["run_shell_command"], "sandbox": False},
        has_outer_sandbox=False,
    )

    assert status == "blocked"
    assert "Gemini sandbox" in str(reason)
    assert isinstance(allowed_tools, list)
    assert "run_shell_command" in allowed_tools
