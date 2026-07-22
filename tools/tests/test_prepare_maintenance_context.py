from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "maintenance_image" / "prepare_context.py"
    spec = importlib.util.spec_from_file_location("prepare_maintenance_context", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_context_copy_has_exact_declared_inputs_and_no_python_cache_artifacts(
    tmp_path: Path,
) -> None:
    module = _load_module()
    repo = tmp_path / "repo"
    project = repo / "packages" / "example"
    _write(
        repo / "tools" / "scaffold" / "monorepo.toml",
        "schema_version = 1\n\n[[projects]]\nid = 'example'\npath = 'packages/example'\n",
    )
    _write(
        repo / "configs" / "agents.yaml",
        "agents:\n"
        "  codex: {sandbox_cli_install: {apt: [], pip: [], npm_global: []}}\n"
        "  claude: {sandbox_cli_install: {apt: [], pip: [], npm_global: []}}\n"
        "  gemini: {sandbox_cli_install: {apt: [], pip: [], npm_global: []}}\n",
    )
    _write(project / "pyproject.toml", "[project]\nname = 'example'\nversion = '0'\n")
    _write(project / "pdm.lock", "# lock\n")
    _write(project / "README.md", "not a runtime input\n")
    _write(project / "src" / "example" / "__init__.py", "VALUE = 1\n")
    _write(project / "src" / "example" / "module.pyc", "compiled\n")
    _write(project / "src" / "example" / "module.pyo", "optimized\n")
    _write(
        project / "src" / "example" / "__pycache__" / "cached.pyc",
        "cached\n",
    )
    contexts = (
        repo
        / "packages"
        / "sandbox_runner"
        / "src"
        / "sandbox_runner"
        / "builtins"
        / "docker"
        / "contexts"
    )
    base = contexts / "sandbox_cli"
    maintenance = contexts / "sandbox_cli_maintenance"
    _write(base / "Dockerfile", "FROM python:3.13-slim\n")
    _write(base / "manifests" / "pip.txt", "pdm==2.25.9\n")
    _write(base / "__pycache__" / "base.pyc", "cached\n")
    _write(maintenance / "overlay.txt", "maintenance\n")
    _write(maintenance / "overlay.pyo", "optimized\n")

    output = tmp_path / "context"
    metadata = module.prepare_context(repo_root=repo, output_dir=output)

    assert metadata["copied_paths"] == [
        "configs",
        "packages/example/README.md",
        "packages/example/pdm.lock",
        "packages/example/pyproject.toml",
        "packages/example/src",
        "tools/scaffold",
    ]
    assert (output / "repo_snapshot" / "packages" / "example" / "README.md").is_file()
    copied_files = [path for path in output.rglob("*") if path.is_file()]
    assert all("__pycache__" not in path.parts for path in copied_files)
    assert all(path.suffix.casefold() not in {".pyc", ".pyo"} for path in copied_files)
    assert (output / "repo_snapshot" / "packages" / "example" / "src" / "example" / "__init__.py").is_file()
