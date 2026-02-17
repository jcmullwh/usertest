from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


ALLOWED_STATUSES = {"internal", "incubator", "supported", "stable"}


class DiscoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class PythonPackage:
    path: Path
    name: str
    base_version: str
    status: str


def _read_pyproject(pyproject_path: Path) -> dict:
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise DiscoverError(f"Missing pyproject.toml: {pyproject_path}") from e
    except Exception as e:  # pragma: no cover - defensive, error text varies
        raise DiscoverError(f"Failed to parse pyproject.toml: {pyproject_path}: {e}") from e
    if not isinstance(data, dict):
        raise DiscoverError(f"Unexpected pyproject.toml shape (expected dict): {pyproject_path}")
    return data


def _get_status(pyproject: dict) -> str:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        tool = {}
    monorepo = tool.get("monorepo")
    if not isinstance(monorepo, dict):
        monorepo = {}
    status = monorepo.get("status", "internal")
    if status is None:
        status = "internal"
    if not isinstance(status, str):
        raise DiscoverError("Invalid [tool.monorepo].status (expected string).")
    if status not in ALLOWED_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_STATUSES))
        raise DiscoverError(f"Unsupported [tool.monorepo].status={status!r} (allowed: {allowed}).")
    return status


def discover_python_packages(repo_root: Path) -> list[PythonPackage]:
    packages_dir = repo_root / "packages"
    if not packages_dir.exists():
        raise DiscoverError(f"Expected packages/ directory at: {packages_dir}")

    out: list[PythonPackage] = []
    for child in sorted(packages_dir.iterdir()):
        if not child.is_dir():
            continue
        pyproject_path = child / "pyproject.toml"
        if not pyproject_path.exists():
            continue

        pyproject = _read_pyproject(pyproject_path)
        project = pyproject.get("project")
        if not isinstance(project, dict):
            raise DiscoverError(f"Missing/invalid [project] table in {pyproject_path}")

        name = project.get("name")
        if not isinstance(name, str) or not name.strip():
            raise DiscoverError(f"Missing/invalid [project].name in {pyproject_path}")

        version = project.get("version")
        if not isinstance(version, str) or not version.strip():
            raise DiscoverError(f"Missing/invalid [project].version in {pyproject_path}")

        status = _get_status(pyproject)
        out.append(PythonPackage(path=child, name=name, base_version=version, status=status))

    return out
