from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from onboarding_contract import ONBOARDING_CONTRACT, command_path


def test_docs_and_readmes_follow_onboarding_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    getting_started = (repo_root / "docs" / "tutorials" / "getting-started.md").read_text(
        encoding="utf-8"
    )
    monorepo_setup = (repo_root / "docs" / "tutorials" / "monorepo-setup.md").read_text(
        encoding="utf-8"
    )
    scripts_readme = (repo_root / "scripts" / "README.md").read_text(encoding="utf-8")
    how_to = (repo_root / "docs" / "how-to" / "run-usertest.md").read_text(encoding="utf-8")
    adr = (repo_root / "docs" / "design" / "adr_usertest_smoke_command.md").read_text(
        encoding="utf-8"
    )

    canonical = ONBOARDING_CONTRACT.canonical_path
    doctor = command_path("doctor")
    smoke = command_path("smoke")
    first_real_run = ONBOARDING_CONTRACT.first_real_run

    assert canonical.precedence in readme
    assert canonical.precedence in getting_started
    assert "Developer smoke" in getting_started

    for command in canonical.commands:
        assert command.command in readme
        assert command.command in getting_started
        assert command.command in scripts_readme
        assert command.command in adr

    for command in doctor.commands:
        assert command.command in readme
        assert command.command in getting_started
        assert command.command in adr

    for command in smoke.commands:
        assert command.command in readme
        assert command.command in getting_started
        assert command.command in monorepo_setup
        assert command.command in adr

    assert first_real_run.preferred_command in readme
    assert first_real_run.preferred_command in getting_started
    assert first_real_run.alternate_command in readme
    assert first_real_run.alternate_command in getting_started
    assert first_real_run.alternate_command in how_to
    assert "canonical first real run" in how_to


def test_smoke_import_guard_remediation_uses_contract_commands(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_pkg = tmp_path / "shadow" / "usertest"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text("__all__ = []\n", encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_pkg.parent)

    proc = subprocess.run(
        [sys.executable, "tools/smoke_import_guard.py", "--repo-root", str(repo_root)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 3
    assert command_path("doctor").command_for("windows_powershell") in proc.stderr
    assert command_path("doctor").command_for("posix_bash") in proc.stderr
    windows_smoke_pythonpath = (
        f"{command_path('smoke').command_for('windows_powershell')} -UsePythonPath"
    )
    assert windows_smoke_pythonpath in proc.stderr
    assert f"{command_path('smoke').command_for('posix_bash')} --use-pythonpath" in proc.stderr
