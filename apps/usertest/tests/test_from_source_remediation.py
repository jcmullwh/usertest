from __future__ import annotations

from onboarding_contract import ONBOARDING_CONTRACT

from usertest.cli import _from_source_import_remediation


def test_from_source_import_remediation_mentions_supported_fixes() -> None:
    msg = _from_source_import_remediation(missing_module="agent_adapters")
    assert "requirements-dev.txt" in msg
    for command in ONBOARDING_CONTRACT.canonical_path.commands:
        assert command.command in msg
    assert "scripts\\set_pythonpath.ps1" in msg
    assert "scripts/set_pythonpath.sh" in msg
