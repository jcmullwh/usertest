from __future__ import annotations

from pathlib import Path

import pytest
from runner_core import find_repo_root

from usertest.cli import main


def test_init_usertest_writes_scaffold_and_is_non_destructive(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    with pytest.raises(SystemExit) as exc:
        main(["init-usertest", "--repo-root", str(repo_root), "--repo", str(tmp_path)])
    assert exc.value.code == 0

    usertest_dir = tmp_path / ".usertest"
    catalog = usertest_dir / "catalog.yaml"
    manifest = usertest_dir / "sandbox_cli_install.yaml"
    assert usertest_dir.exists()
    assert catalog.exists()
    assert manifest.exists()
    assert "version: 1" in catalog.read_text(encoding="utf-8")
    assert "defaults:" in catalog.read_text(encoding="utf-8")
    assert "sandbox_cli_install:" in manifest.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc2:
        main(["init-usertest", "--repo-root", str(repo_root), "--repo", str(tmp_path)])
    assert exc2.value.code == 2

    with pytest.raises(SystemExit) as exc3:
        main(
            [
                "init-usertest",
                "--repo-root",
                str(repo_root),
                "--repo",
                str(tmp_path),
                "--force",
            ]
        )
    assert exc3.value.code == 0
