from __future__ import annotations

from pathlib import Path


def test_smoke_scripts_exist_and_enforce_expected_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
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

        if path.name == "smoke.sh":
            assert "Smoke preflight failed:" in text
            assert "Choose one setup mode:" in text
            skip_preflight_index = text.index('if [[ "${SKIP_INSTALL}" -eq 1 ]]; then')
            guard_index = text.index('echo "==> Import-origin guard smoke"')
            assert skip_preflight_index < guard_index
        if path.name == "smoke.ps1":
            assert "Smoke preflight failed:" in text
            assert "Choose one setup mode:" in text
            skip_preflight_index = text.index("if ($SkipInstall)")
            guard_index = text.index("Write-Host '==> Import-origin guard smoke'")
            assert skip_preflight_index < guard_index
