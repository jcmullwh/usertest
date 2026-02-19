from __future__ import annotations

import os
from pathlib import Path

from runner_core.runner import _effective_gemini_cli_sandbox


def _has_dockerenv_marker() -> bool:
    try:
        return Path("/.dockerenv").exists()
    except OSError:
        return False


def test_effective_gemini_cli_sandbox_disables_when_outer_sandbox_present() -> None:
    assert _effective_gemini_cli_sandbox(policy_value=True, has_outer_sandbox=True) is False
    assert _effective_gemini_cli_sandbox(policy_value=False, has_outer_sandbox=True) is False


def test_effective_gemini_cli_sandbox_obeys_policy_locally() -> None:
    expected_enabled = os.name != "nt" and not _has_dockerenv_marker()
    assert (
        _effective_gemini_cli_sandbox(policy_value=True, has_outer_sandbox=False)
        is expected_enabled
    )
    assert _effective_gemini_cli_sandbox(policy_value=False, has_outer_sandbox=False) is False


def test_effective_gemini_cli_sandbox_defaults_true_for_non_bool_policy_values() -> None:
    expected_enabled = os.name != "nt" and not _has_dockerenv_marker()
    assert (
        _effective_gemini_cli_sandbox(policy_value="true", has_outer_sandbox=False)
        is expected_enabled
    )
    assert (
        _effective_gemini_cli_sandbox(policy_value=None, has_outer_sandbox=False)
        is expected_enabled
    )
