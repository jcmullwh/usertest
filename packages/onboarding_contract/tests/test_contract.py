from __future__ import annotations

from onboarding_contract import ONBOARDING_CONTRACT, command_path, render_first_success_remediation


def test_first_success_remediation_renders_contract_commands() -> None:
    msg = render_first_success_remediation()
    canonical = ONBOARDING_CONTRACT.canonical_path

    assert "Quick fix (recommended)" in msg
    for command in canonical.commands:
        assert command.command in msg


def test_command_path_lookup_returns_shared_paths() -> None:
    assert command_path("offline_first_success") == ONBOARDING_CONTRACT.canonical_path
    assert command_path("doctor").title == "Doctor"
    assert command_path("smoke").title == "Developer smoke"
