from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.scaffold.install_cache_common import normalize_pdm_version


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_install_cache_fingerprint_cli_reports_manifest_projects(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "tools" / "scaffold" / "monorepo.toml"
    _write(
        manifest_path,
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[projects]]",
                'id = "demo"',
                'path = "packages/demo"',
                'tasks.install = ["pdm", "install"]',
                "",
            ]
        ),
    )
    _write(
        repo_root / "packages" / "demo" / "pyproject.toml",
        "[project]\nname='demo'\nversion='0.1.0'\n",
    )
    _write(repo_root / "packages" / "demo" / "pdm.lock", 'lock-version = "4.5.0"\n')

    script_path = Path(__file__).resolve().with_name("install_cache_fingerprint.py")
    proc = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--repo-root",
            str(repo_root),
            "--all",
            "--python-major-minor",
            "3.11",
            "--pdm-version",
            "2.26.2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == 1
    assert payload["repo_root"] == str(repo_root.resolve())
    assert payload["python_major_minor"] == "3.11"
    assert payload["pdm_version"] == "2.26.2"
    assert payload["projects"] == [
        {
            "id": "demo",
            "path": "packages/demo",
            "fingerprint": payload["projects"][0]["fingerprint"],
            "payload": {
                "schema_version": 1,
                "project_id": "demo",
                "project_path": "packages/demo",
                "pyproject_sha256": payload["projects"][0]["payload"]["pyproject_sha256"],
                "pdm_lock_sha256": payload["projects"][0]["payload"]["pdm_lock_sha256"],
                "python_major_minor": "3.11",
                "pdm_version": "2.26.2",
                "install_cmd": ["pdm", "install"],
            },
        }
    ]


def test_normalize_pdm_version_accepts_cli_banner() -> None:
    assert normalize_pdm_version("PDM, version 2.26.2") == "2.26.2"
    assert normalize_pdm_version(" 2.26.2 ") == "2.26.2"
