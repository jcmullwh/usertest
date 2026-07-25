from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_smoke_import_guard_detects_shadowing(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    guard_py = repo_root / "tools" / "smoke_import_guard.py"
    assert guard_py.exists()

    fake_pkg = tmp_path / "usertest"
    fake_pkg.mkdir(parents=True, exist_ok=True)
    (fake_pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)

    res = subprocess.run(
        [sys.executable, str(guard_py), "--repo-root", str(repo_root)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )

    assert res.returncode == 3, res.stderr or res.stdout
    assert "Import-origin guard (usertest)" in res.stdout
    assert "usertest.__file__:" in res.stdout
    assert str(repo_root) in (res.stderr + res.stdout)
    assert str(fake_pkg) in (res.stderr + res.stdout)
    assert "import shadowing detected" in res.stderr


def test_smoke_import_guard_allows_workspace_usertest() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    guard_py = repo_root / "tools" / "smoke_import_guard.py"
    assert guard_py.exists()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)

    res = subprocess.run(
        [sys.executable, str(guard_py), "--repo-root", str(repo_root)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, res.stderr or res.stdout
    assert "OK: usertest resolves within this workspace." in res.stdout

