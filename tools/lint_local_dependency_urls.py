from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_LOCAL_URL_PREFIX = "file:///${PROJECT_ROOT}/"
_CANONICAL_MONOREPO_PREFIX = "../../packages/"
_REQUIRED_USERTEST_LOCAL_DEPS = frozenset(
    canonicalize_name(name)
    for name in (
        "agent-adapters",
        "normalized_events",
        "reporter",
        "runner_core",
        "sandbox_runner",
    )
)


def _iter_project_pyprojects(repo_root: Path) -> list[Path]:
    pyprojects: list[Path] = []
    for group in ("apps", "packages"):
        group_dir = repo_root / group
        if not group_dir.exists():
            continue
        for candidate in sorted(group_dir.iterdir()):
            if not candidate.is_dir():
                continue
            pyproject = candidate / "pyproject.toml"
            if pyproject.exists():
                pyprojects.append(pyproject)
    return pyprojects


def _load_toml(path: Path) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("TOML root must be a table")
    return data


def _iter_project_dependency_strings(pyproject_data: dict) -> list[str]:
    project = pyproject_data.get("project")
    if not isinstance(project, dict):
        return []

    deps = project.get("dependencies")
    if not isinstance(deps, list):
        return []
    return [str(dep) for dep in deps]


def _validate_local_url_requirement(
    *,
    req: Requirement,
    req_s: str,
    pyproject_path: Path,
    project_root: Path,
    packages_root: Path,
) -> list[str]:
    errors: list[str] = []
    if req.url is None or not req.url.startswith("file:"):
        return errors

    if not req.url.startswith(_LOCAL_URL_PREFIX):
        errors.append(
            "local_url_must_use_project_root_macro: "
            f"{pyproject_path}: {req_s!r} (expected prefix {_LOCAL_URL_PREFIX!r})"
        )
        return errors

    relative_url_path = req.url[len(_LOCAL_URL_PREFIX) :]
    if not relative_url_path.startswith(_CANONICAL_MONOREPO_PREFIX):
        errors.append(
            "local_url_must_route_via_monorepo_root: "
            f"{pyproject_path}: {req_s!r} "
            f"(expected path prefix {_CANONICAL_MONOREPO_PREFIX!r})"
        )
        return errors

    target_dir = (project_root / Path(relative_url_path)).resolve()
    if not target_dir.exists():
        errors.append(f"local_dep_target_missing: {pyproject_path}: {req_s!r} -> {target_dir}")
        return errors

    if not target_dir.is_relative_to(packages_root):
        errors.append(
            f"local_dep_target_not_under_packages: {pyproject_path}: {req_s!r} -> {target_dir}"
        )
        return errors

    target_pyproject = target_dir / "pyproject.toml"
    if not target_pyproject.exists():
        errors.append(
            "local_dep_target_missing_pyproject: "
            f"{pyproject_path}: {req_s!r} -> {target_pyproject}"
        )
        return errors

    try:
        target_data = _load_toml(target_pyproject)
        target_project = target_data.get("project")
        target_name = target_project.get("name") if isinstance(target_project, dict) else None
    except Exception as exc:
        errors.append(f"local_dep_target_invalid_pyproject: {target_pyproject}: {exc}")
        return errors

    if not isinstance(target_name, str) or not target_name.strip():
        errors.append(f"local_dep_target_missing_name: {target_pyproject}")
        return errors

    if canonicalize_name(req.name) != canonicalize_name(target_name):
        errors.append(
            "local_dep_name_mismatch: "
            f"{pyproject_path}: {req.name!r} points to {target_pyproject} "
            f"with project.name={target_name!r}"
        )
    return errors


def lint_local_dependency_urls(*, repo_root: Path) -> list[str]:
    errors: list[str] = []
    packages_root = (repo_root / "packages").resolve()
    usertest_pyproject = repo_root / "apps" / "usertest" / "pyproject.toml"

    for pyproject_path in _iter_project_pyprojects(repo_root):
        project_root = pyproject_path.parent
        is_package = pyproject_path.is_relative_to(packages_root)
        is_usertest_app = pyproject_path.resolve() == usertest_pyproject.resolve()

        try:
            pyproject_data = _load_toml(pyproject_path)
        except Exception as exc:
            errors.append(f"invalid_pyproject: {pyproject_path}: {exc}")
            continue

        seen_usertest_internal: set[str] = set()
        for req_s in _iter_project_dependency_strings(pyproject_data):
            try:
                req = Requirement(req_s)
            except Exception as exc:
                errors.append(f"invalid_requirement: {pyproject_path}: {req_s!r}: {exc}")
                continue

            if is_package and req.url is not None and req.url.startswith("file:"):
                errors.append(
                    "package_runtime_dep_must_not_use_file_url: "
                    f"{pyproject_path}: {req_s!r}"
                )
                continue

            errors.extend(
                _validate_local_url_requirement(
                    req=req,
                    req_s=req_s,
                    pyproject_path=pyproject_path,
                    project_root=project_root,
                    packages_root=packages_root,
                )
            )

            dep_name = canonicalize_name(req.name)
            if is_usertest_app and dep_name in _REQUIRED_USERTEST_LOCAL_DEPS:
                seen_usertest_internal.add(dep_name)
                if req.url is None or not req.url.startswith("file:"):
                    errors.append(
                        "usertest_internal_dep_must_use_local_url: "
                        f"{pyproject_path}: {req_s!r}"
                    )

        if is_usertest_app:
            missing = sorted(_REQUIRED_USERTEST_LOCAL_DEPS - seen_usertest_internal)
            if missing:
                errors.append(
                    "usertest_missing_required_local_deps: "
                    f"{pyproject_path}: {', '.join(missing)}"
                )

    return errors


def main(argv: list[str] | None = None) -> int:
    _ = argv
    repo_root = Path(__file__).resolve().parents[1]
    errors = lint_local_dependency_urls(repo_root=repo_root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
