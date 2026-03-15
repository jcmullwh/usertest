from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_smoke_scripts_exist_and_enforce_expected_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / "scripts" / "python_preflight.sh").exists()
    assert (repo_root / "scripts" / "python_preflight.ps1").exists()
    scripts = [
        repo_root / "scripts" / "smoke.ps1",
        repo_root / "scripts" / "smoke.sh",
    ]

    for path in scripts:
        assert path.exists(), f"missing smoke script: {path}"
        text = path.read_text(encoding="utf-8")
        assert "usertest.cli --help" in text
        assert "usertest_backlog.cli --help" in text
        assert "apps/usertest/tests/test_smoke.py" in text
        assert "apps/usertest/tests/test_golden_fixture.py" in text
        assert "apps/usertest_backlog/tests/test_smoke.py" in text
        assert "packages/run_artifacts" in text
        assert "pip install -U pdm" in text
        assert "tools/smoke_import_guard.py" in text
        assert "USERTEST_PYTHON" in text

        if path.name == "smoke.sh":
            assert 'source "${SCRIPT_DIR}/python_preflight.sh"' in text
            assert "Smoke preflight failed:" in text
            assert "Choose one setup mode:" in text
            guard_idx = text.find('echo "==> Import-origin guard smoke"')
            preflight_call_idx = text.find("  run_skip_install_preflight")
            assert preflight_call_idx != -1
            assert guard_idx != -1
            assert preflight_call_idx < guard_idx
        else:
            assert "Resolve-UsablePython -RepoRoot $repoRoot" in text
            guard_idx = text.find("Write-Host '==> Import-origin guard smoke'")
            preflight_call_idx = text.find(
                "        Invoke-SmokeImportPreflight -PythonCmd $pythonCmd"
            )
            assert preflight_call_idx != -1
            assert guard_idx != -1
            assert preflight_call_idx < guard_idx


def test_python_preflight_ps1_hardens_failure_output_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / "scripts" / "python_preflight.ps1").read_text(encoding="utf-8")

    assert 'ReasonCode = "access_denied"' in text
    assert 'ReasonCode = "missing_stdlib"' in text
    assert "_Is-WindowsAppsAliasPath" in text
    assert "Write-PreflightErr" in text
    assert 'No usable Python interpreter found (within ~$TimeoutSeconds seconds).' in text
    assert "return $null" in text
    assert "throw ($lines -join" not in text


@pytest.mark.parametrize(
    "script_name",
    [
        "smoke.ps1",
        "doctor.ps1",
        "snapshot_repo.ps1",
        "offline_first_success.ps1",
    ],
)
def test_powershell_wrappers_stop_immediately_after_preflight_failure(
    script_name: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / "scripts" / script_name).read_text(encoding="utf-8")

    match = re.search(
        r"\$pythonInfo = Resolve-UsablePython -RepoRoot \$repoRoot\s+"
        r"if \(-not \$pythonInfo\) \{\s+exit 1\s+\}\s+"
        r"\$pythonCmd = \$pythonInfo\.CommandPath",
        text,
    )
    assert match, f"{script_name} should exit before using $pythonInfo on preflight failure"


def test_smoke_ps1_parse_preflight_when_powershell_is_available() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell runtime not available in this environment")

    command = [powershell, "-NoProfile"]
    if Path(powershell).name.lower().startswith("powershell"):
        command += ["-ExecutionPolicy", "Bypass"]
    command += [
        "-File",
        str(repo_root / "scripts" / "parse_preflight.ps1"),
        str(repo_root / "scripts" / "smoke.ps1"),
    ]

    proc = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "PowerShell parse OK" in proc.stdout
