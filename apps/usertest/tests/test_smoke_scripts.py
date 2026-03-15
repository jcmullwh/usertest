from __future__ import annotations

from pathlib import Path


def test_first_run_wrappers_delegate_to_shared_launcher_contract() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    launcher = repo_root / "tools" / "first_run_launcher.py"
    launcher_module = repo_root / "apps" / "usertest" / "src" / "usertest" / "first_run_launcher.py"
    assert launcher.exists()
    assert launcher_module.exists()

    smoke_wrappers = [
        repo_root / "scripts" / "smoke.ps1",
        repo_root / "scripts" / "smoke.sh",
    ]
    onboarding_wrappers = [
        repo_root / "scripts" / "offline_first_success.ps1",
        repo_root / "scripts" / "offline_first_success.sh",
    ]

    for path in smoke_wrappers:
        text = path.read_text(encoding="utf-8")
        assert "tools/first_run_launcher.py" in text
        assert "smoke" in text
        assert "USERTEST_PYTHON" in text
        if path.suffix == ".sh":
            assert 'source "${SCRIPT_DIR}/python_preflight.sh"' in text
            assert "--shell posix" in text
        else:
            assert "Resolve-UsablePython -RepoRoot $repoRoot" in text
            assert "'powershell'" in text
        assert "usertest.cli --help" not in text
        assert "Smoke preflight failed:" not in text

    for path in onboarding_wrappers:
        text = path.read_text(encoding="utf-8")
        assert "tools/first_run_launcher.py" in text
        assert "offline-first-success" in text
        assert "USERTEST_PYTHON" in text
        if path.suffix == ".sh":
            assert 'source "${SCRIPT_DIR}/python_preflight.sh"' in text
            assert "--shell posix" in text
        else:
            assert "Resolve-UsablePython -RepoRoot $repoRoot" in text
            assert "'powershell'" in text
            assert "Write-Err" not in text

    launcher_text = launcher_module.read_text(encoding="utf-8")
    assert "Smoke preflight failed:" in launcher_text
    assert "tools/smoke_import_guard.py" in launcher_text
    assert "usertest.cli" in launcher_text
    assert "usertest_backlog.cli" in launcher_text
    assert "usertest_implement.cli" in launcher_text
    assert "apps/usertest/tests/test_smoke.py" in launcher_text
    assert "apps/usertest/tests/test_golden_fixture.py" in launcher_text
    assert "apps/usertest_backlog/tests/test_smoke.py" in launcher_text
    assert "apps/usertest_implement/tests/test_smoke.py" in launcher_text
    assert "pip install -U pdm" in launcher_text
