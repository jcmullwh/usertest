from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "check_package_readme_contract.py"
    spec = importlib.util.spec_from_file_location("check_package_readme_contract", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_readme_text_accepts_standalone_and_monorepo_sections() -> None:
    module = _load_module()
    readme = "\n".join(
        [
            "# Example",
            "",
            "## Standalone package checkout (recommended first path)",
            "",
            "```bash",
            "pdm run smoke",
            "pdm run test",
            "pdm run lint",
            "pip install --index-url https://example.test/api/v4/projects/1/packages/pypi/simple pkg",
            "```",
            "",
            "## Monorepo contributor workflow",
            "",
            "```bash",
            "python tools/scaffold/scaffold.py run test --project example",
            "```",
            "",
        ]
    )
    issues = module.validate_readme_text(readme_path=Path("packages/example/README.md"), text=readme)
    assert issues == []


def test_validate_readme_text_rejects_missing_required_headings() -> None:
    module = _load_module()
    readme = "# Example\n\n## Development\n\nRun commands.\n"
    issues = module.validate_readme_text(readme_path=Path("packages/example/README.md"), text=readme)
    assert any("Standalone package checkout" in issue for issue in issues)
    assert any("Monorepo contributor workflow" in issue for issue in issues)


def test_validate_readme_text_rejects_monorepo_commands_in_standalone_section() -> None:
    module = _load_module()
    readme = "\n".join(
        [
            "# Example",
            "",
            "## Standalone package checkout (recommended first path)",
            "",
            "```bash",
            "pdm run smoke",
            "pdm run test",
            "pdm run lint",
            "python tools/scaffold/scaffold.py run test --project example",
            "```",
            "",
            "## Monorepo contributor workflow",
            "Use scaffold here.",
            "",
        ]
    )
    issues = module.validate_readme_text(readme_path=Path("packages/example/README.md"), text=readme)
    assert any("forbidden monorepo scaffold command" in issue for issue in issues)


def test_discover_package_readmes_finds_only_package_dirs_with_pyproject(tmp_path: Path) -> None:
    module = _load_module()
    pkg_a = tmp_path / "packages" / "pkg_a"
    pkg_b = tmp_path / "packages" / "pkg_b"
    docs = tmp_path / "packages" / "docs_only"
    pkg_a.mkdir(parents=True)
    pkg_b.mkdir(parents=True)
    docs.mkdir(parents=True)
    (pkg_a / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")
    (pkg_a / "README.md").write_text("# a\n", encoding="utf-8")
    (pkg_b / "README.md").write_text("# b\n", encoding="utf-8")
    (docs / "README.md").write_text("# docs\n", encoding="utf-8")

    readmes = module.discover_package_readmes(tmp_path)
    assert readmes == [pkg_a / "README.md"]
