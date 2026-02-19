from __future__ import annotations

from pathlib import Path

from runner_core import find_repo_root


def test_smoke_scripts_exist_and_enforce_expected_contract() -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
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
