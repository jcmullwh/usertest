from __future__ import annotations

from onboarding_contract import render_first_success_remediation

from usertest_implement.cli import _from_source_import_remediation


def test_implement_from_source_remediation_uses_shared_onboarding_contract() -> None:
    msg = _from_source_import_remediation(missing_module="runner_core")

    assert render_first_success_remediation() in msg
    assert "requirements-dev.txt" in msg
