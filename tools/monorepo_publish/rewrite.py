from __future__ import annotations

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from tomlkit import parse as toml_parse
from tomlkit import dumps as toml_dumps


class RewriteError(RuntimeError):
    pass


def _format_pinned(req: Requirement, *, version: str) -> str:
    extras = ""
    if req.extras:
        extras = "[" + ",".join(sorted(req.extras)) + "]"
    marker = f"; {req.marker}" if req.marker is not None else ""
    return f"{req.name}{extras}=={version}{marker}"


def _rewrite_req(req_s: str, *, package_versions: dict[str, str], self_name: str) -> str:
    try:
        req = Requirement(req_s)
    except Exception as e:
        raise RewriteError(f"Invalid requirement string: {req_s!r}: {e}") from e

    dep_key = canonicalize_name(req.name)
    if dep_key == self_name:
        return req_s

    if dep_key in package_versions:
        return _format_pinned(req, version=package_versions[dep_key])

    if req.url is not None and req.url.startswith("file:"):
        raise RewriteError(
            "Snapshot publishing cannot include local path requirements. "
            f"Found: {req_s!r}. If this is an internal dependency, mark the dependency package "
            "as non-internal for publishing; otherwise replace it with a versioned dependency."
        )

    return req_s


def rewrite_pyproject_for_snapshot(pyproject_path: Path, package_versions: dict[str, str]) -> None:
    try:
        doc = toml_parse(pyproject_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RewriteError(f"Missing pyproject.toml: {pyproject_path}") from e
    except Exception as e:
        raise RewriteError(f"Failed to parse pyproject.toml: {pyproject_path}: {e}") from e

    project = doc.get("project")
    if project is None or not isinstance(project, dict):
        raise RewriteError(f"Missing/invalid [project] table: {pyproject_path}")

    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RewriteError(f"Missing/invalid [project].name: {pyproject_path}")

    self_key = canonicalize_name(name)
    new_version = package_versions.get(self_key)
    if new_version is None:
        raise RewriteError(
            f"Package {name!r} was not assigned a snapshot version. "
            "This usually means it was not considered eligible for publishing."
        )
    project["version"] = new_version

    deps = project.get("dependencies")
    if deps is not None:
        if not isinstance(deps, list):
            raise RewriteError(f"Invalid [project].dependencies (expected array): {pyproject_path}")
        project["dependencies"] = [
            _rewrite_req(str(req_s), package_versions=package_versions, self_name=self_key)
            for req_s in deps
        ]

    opt = project.get("optional-dependencies")
    if opt is not None:
        if not isinstance(opt, dict):
            raise RewriteError(f"Invalid [project].optional-dependencies (expected table): {pyproject_path}")
        for group, group_deps in list(opt.items()):
            if not isinstance(group_deps, list):
                raise RewriteError(
                    f"Invalid [project].optional-dependencies.{group} (expected array): {pyproject_path}"
                )
            opt[group] = [
                _rewrite_req(str(req_s), package_versions=package_versions, self_name=self_key)
                for req_s in group_deps
            ]

    pyproject_path.write_text(toml_dumps(doc), encoding="utf-8")
