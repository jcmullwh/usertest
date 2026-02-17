from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def _load_scaffold_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "tools" / "scaffold" / "scaffold.py"
    spec = importlib.util.spec_from_file_location("scaffold_tool", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scaffold_run_skip_missing_skips_undefined_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scaffold = _load_scaffold_module()

    fake_root = tmp_path / "repo"
    (fake_root / "tools" / "scaffold").mkdir(parents=True, exist_ok=True)
    (fake_root / "packages" / "a").mkdir(parents=True, exist_ok=True)
    (fake_root / "packages" / "b").mkdir(parents=True, exist_ok=True)

    (fake_root / "tools" / "scaffold" / "monorepo.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[projects]]",
                'id = "a"',
                'kind = "lib"',
                'path = "packages/a"',
                'tasks.lint = ["python", "-c", "print(\'ok\')"]',
                "",
                "[[projects]]",
                'id = "b"',
                'kind = "lib"',
                'path = "packages/b"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(scaffold, "_repo_root", lambda: fake_root)

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(scaffold, "_run", _fake_run)

    args = scaffold.argparse.Namespace(
        task="lint",
        all=True,
        kind=None,
        project=None,
        skip_missing=True,
        keep_going=False,
    )

    assert scaffold.cmd_run(args) == 0
    assert calls == [["python", "-c", "print('ok')"]]
    captured = capsys.readouterr()
    assert "missing tasks.lint" in captured.err


def test_scaffold_run_without_skip_missing_raises_on_undefined_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold = _load_scaffold_module()

    fake_root = tmp_path / "repo"
    (fake_root / "tools" / "scaffold").mkdir(parents=True, exist_ok=True)
    (fake_root / "packages" / "a").mkdir(parents=True, exist_ok=True)

    (fake_root / "tools" / "scaffold" / "monorepo.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[projects]]",
                'id = "a"',
                'kind = "lib"',
                'path = "packages/a"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(scaffold, "_repo_root", lambda: fake_root)

    args = scaffold.argparse.Namespace(
        task="lint",
        all=True,
        kind=None,
        project=None,
        skip_missing=False,
        keep_going=False,
    )

    with pytest.raises(scaffold.ScaffoldError, match=r"missing tasks\.lint"):
        scaffold.cmd_run(args)


def test_scaffold_golden_path_smoke_add_doctor_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scaffold = _load_scaffold_module()

    fake_root = tmp_path / "repo"
    (fake_root / "tools" / "scaffold").mkdir(parents=True, exist_ok=True)
    template_root = fake_root / "tools" / "templates" / "internal" / "python-stdlib-copy"
    (template_root / "src" / "__NAME_SNAKE__").mkdir(parents=True, exist_ok=True)
    (template_root / "tests").mkdir(parents=True, exist_ok=True)
    (template_root / "README.md").write_text("# __NAME__\n", encoding="utf-8")
    (template_root / "src" / "__NAME_SNAKE__" / "__init__.py").write_text("", encoding="utf-8")
    (template_root / "tests" / "test_basic.py").write_text(
        "\n".join(
            [
                "import unittest",
                "",
                "class SmokeTest(unittest.TestCase):",
                "    def test_truth(self):",
                "        self.assertTrue(True)",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        ),
        encoding="utf-8",
    )

    (fake_root / "tools" / "scaffold" / "registry.toml").write_text(
        "\n".join(
            [
                "[kinds.app]",
                'output_dir = "apps"',
                'default_generator = "python_stdlib_copy"',
                "ci = { lint = true, test = true, build = false }",
                "",
                "[generators.python_stdlib_copy]",
                'type = "copy"',
                'source = "tools/templates/internal/python-stdlib-copy"',
                'toolchain = "python"',
                'package_manager = "none"',
                'substitutions = { "__NAME__" = "{name}", "__NAME_SNAKE__" = "{name_snake}" }',
                'tasks.lint = ["python", "-m", "compileall", "src"]',
                'tasks.test = ["python", "-m", "unittest", "discover", "-s", "tests"]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(scaffold, "_repo_root", lambda: fake_root)

    add_args = scaffold.argparse.Namespace(
        kind="app",
        name="smoke-app",
        generator="python_stdlib_copy",
        no_install=True,
        vars=[],
        trust=False,
        allow_unpinned=False,
        allow_missing_ci_tasks=False,
    )
    assert scaffold.cmd_add(add_args) == 0

    generated_root = fake_root / "apps" / "smoke-app"
    assert generated_root.exists()
    assert (generated_root / "README.md").exists()
    assert (generated_root / "src" / "smoke_app" / "__init__.py").exists()
    assert (generated_root / "tests" / "test_basic.py").exists()

    manifest = tomllib.loads(
        (fake_root / "tools" / "scaffold" / "monorepo.toml").read_text(encoding="utf-8")
    )
    projects = {project["id"]: project for project in manifest["projects"]}
    assert "smoke-app" in projects
    assert projects["smoke-app"]["path"] == "apps/smoke-app"

    assert scaffold.cmd_doctor(scaffold.argparse.Namespace()) == 0

    install_args = scaffold.argparse.Namespace(
        task="install",
        all=False,
        kind=None,
        project=["smoke-app"],
        skip_missing=True,
        keep_going=False,
    )
    assert scaffold.cmd_run(install_args) == 0

    test_args = scaffold.argparse.Namespace(
        task="test",
        all=False,
        kind=None,
        project=["smoke-app"],
        skip_missing=False,
        keep_going=False,
    )
    assert scaffold.cmd_run(test_args) == 0
