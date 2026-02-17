from __future__ import annotations

import tomllib
from pathlib import Path

_DEPTRY_DROPINS_PATTERN = r"^\.agents/dropins/"
_TEMPLATE_PYPROJECTS = (
    "tools/templates/internal/python-pdm-app/{{cookiecutter.project_slug}}/pyproject.toml",
    "tools/templates/internal/python-pdm-lib/{{cookiecutter.project_slug}}/pyproject.toml",
    "tools/templates/internal/python-uv-app/pyproject.toml",
    "tools/templates/internal/python-poetry-app/pyproject.toml",
)


def test_python_templates_exclude_dropins_from_deptry() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for rel_path in _TEMPLATE_PYPROJECTS:
        data = tomllib.loads((repo_root / rel_path).read_text(encoding="utf-8"))
        deptry = data.get("tool", {}).get("deptry", {})
        assert isinstance(deptry, dict), f"{rel_path}: [tool.deptry] missing"
        extend_exclude = deptry.get("extend_exclude")
        assert isinstance(extend_exclude, list), f"{rel_path}: deptry.extend_exclude missing"
        assert _DEPTRY_DROPINS_PATTERN in extend_exclude, (
            f"{rel_path}: deptry.extend_exclude must include {_DEPTRY_DROPINS_PATTERN!r}"
        )
