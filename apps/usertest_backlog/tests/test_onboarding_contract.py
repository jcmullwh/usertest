from __future__ import annotations

from onboarding_contract import render_first_success_remediation

from usertest_backlog.cli import _from_source_import_remediation


def test_backlog_from_source_remediation_uses_shared_onboarding_contract() -> None:
    msg = _from_source_import_remediation(missing_module="backlog_core")

    assert render_first_success_remediation() in msg
    assert "pdm install -d" in msg
