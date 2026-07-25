from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_for_fix_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_fix_backfills_missing_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)

    _write(
        repo_root / "tools/scaffold/registry.toml",
        """
[kinds.lib]
output_dir = "packages"
default_generator = "python_pdm_lib"
ci = { lint = true, test = true, build = false }

[generators.python_pdm_lib]
type = "cookiecutter"
source = "tools/templates/internal/python-pdm-lib"
toolchain = "python"
package_manager = "pdm"
tasks.install = ["pdm", "install"]
tasks.lint = ["pdm", "run", "ruff", "check", "."]
tasks.test = ["pdm", "run", "pytest", "-q"]
""".lstrip(),
    )

    _write(
        repo_root / "tools/scaffold/monorepo.toml",
        """
schema_version = 1

[[projects]]
id = "demo"
kind = "lib"
path = "packages/demo"
generator = "python_pdm_lib"
""".lstrip(),
    )
    (repo_root / "packages/demo").mkdir(parents=True, exist_ok=True)

    rc = scaffold.cmd_fix(argparse.Namespace())
    assert rc == 0

    import tomllib

    data = tomllib.loads((repo_root / "tools/scaffold/monorepo.toml").read_text(encoding="utf-8"))
    projects = {p["id"]: p for p in data.get("projects", [])}
    project = projects["demo"]
    assert project["toolchain"] == "python"
    assert project["package_manager"] == "pdm"
    assert project["ci"] == {"lint": True, "test": True, "build": False}
    assert "tasks" in project
    assert project["tasks"]["install"] == ["pdm", "install"]


def test_fix_does_not_overwrite_tasks_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)

    _write(
        repo_root / "tools/scaffold/registry.toml",
        """
[kinds.lib]
output_dir = "packages"
default_generator = "python_pdm_lib"
ci = { lint = true, test = false, build = false }

[generators.python_pdm_lib]
type = "cookiecutter"
source = "tools/templates/internal/python-pdm-lib"
toolchain = "python"
package_manager = "pdm"
tasks.depcheck = ["pdm", "run", "deptry", "."]
""".lstrip(),
    )

    _write(
        repo_root / "tools/scaffold/monorepo.toml",
        """
schema_version = 1

[[projects]]
id = "demo"
kind = "lib"
path = "packages/demo"
generator = "python_pdm_lib"
toolchain = "python"
package_manager = "pdm"
ci = { lint = true, test = false, build = false }
tasks.depcheck = ["pdm", "run", "deptry", "src"]
""".lstrip(),
    )
    (repo_root / "packages/demo").mkdir(parents=True, exist_ok=True)

    rc = scaffold.cmd_fix(argparse.Namespace())
    assert rc == 0

    import tomllib

    data = tomllib.loads((repo_root / "tools/scaffold/monorepo.toml").read_text(encoding="utf-8"))
    project = next(p for p in data.get("projects", []) if p["id"] == "demo")
    assert project["tasks"]["depcheck"] == ["pdm", "run", "deptry", "src"]


def test_fix_sync_tasks_overwrites_generator_tasks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)

    _write(
        repo_root / "tools/scaffold/registry.toml",
        """
[kinds.lib]
output_dir = "packages"
default_generator = "python_pdm_lib"
ci = { lint = true, test = false, build = false }

[generators.python_pdm_lib]
type = "cookiecutter"
source = "tools/templates/internal/python-pdm-lib"
toolchain = "python"
package_manager = "pdm"
tasks.depcheck = ["pdm", "run", "deptry", "."]
""".lstrip(),
    )

    _write(
        repo_root / "tools/scaffold/monorepo.toml",
        """
schema_version = 1

[[projects]]
id = "demo"
kind = "lib"
path = "packages/demo"
generator = "python_pdm_lib"
toolchain = "python"
package_manager = "pdm"
ci = { lint = true, test = false, build = false }
tasks.depcheck = ["pdm", "run", "deptry", "src"]
""".lstrip(),
    )
    (repo_root / "packages/demo").mkdir(parents=True, exist_ok=True)

    rc = scaffold.cmd_fix(argparse.Namespace(sync_tasks=True))
    assert rc == 0

    import tomllib

    data = tomllib.loads((repo_root / "tools/scaffold/monorepo.toml").read_text(encoding="utf-8"))
    project = next(p for p in data.get("projects", []) if p["id"] == "demo")
    assert project["tasks"]["depcheck"] == ["pdm", "run", "deptry", "."]


def test_fix_check_does_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)

    _write(
        repo_root / "tools/scaffold/registry.toml",
        """
[kinds.lib]
output_dir = "packages"
default_generator = "python_pdm_lib"
ci = { lint = true, test = false, build = false }

[generators.python_pdm_lib]
type = "cookiecutter"
source = "tools/templates/internal/python-pdm-lib"
toolchain = "python"
package_manager = "pdm"
""".lstrip(),
    )

    manifest_path = repo_root / "tools/scaffold/monorepo.toml"
    _write(
        manifest_path,
        """
schema_version = 1

[[projects]]
id = "demo"
kind = "lib"
path = "packages/demo"
generator = "python_pdm_lib"
""".lstrip(),
    )
    before = manifest_path.read_text(encoding="utf-8")

    rc = scaffold.cmd_fix(argparse.Namespace(check=True))
    assert rc == 1
    assert manifest_path.read_text(encoding="utf-8") == before

