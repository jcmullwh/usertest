#!/usr/bin/env python
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Python 3.11+ is required for install_cache_fingerprint.py.") from exc

def _load_install_cache_common() -> Any:
    try:
        from tools.scaffold import install_cache_common as module

        return module
    except ModuleNotFoundError:
        pass

    module_path = Path(__file__).resolve().with_name("install_cache_common.py")
    spec = importlib.util.spec_from_file_location("install_cache_common", module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Unable to load install_cache_common from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


build_install_cache_fingerprint = _load_install_cache_common().build_install_cache_fingerprint


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / "tools" / "scaffold" / "monorepo.toml"


def _load_projects(repo_root: Path) -> list[dict[str, Any]]:
    manifest_path = _manifest_path(repo_root)
    if not manifest_path.exists():
        raise SystemExit(f"Missing scaffold manifest: {manifest_path}")
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    projects = data.get("projects", [])
    if not isinstance(projects, list):
        raise SystemExit(f"Invalid scaffold manifest: expected [[projects]] in {manifest_path}")
    normalized: list[dict[str, Any]] = []
    for idx, project in enumerate(projects):
        if not isinstance(project, dict):
            raise SystemExit(f"Invalid scaffold manifest entry at index {idx}: expected table")
        normalized.append(cast(dict[str, Any], project))
    return normalized


def _select_projects(*, repo_root: Path, project_id: str | None) -> list[dict[str, Any]]:
    projects = _load_projects(repo_root)
    if project_id is None:
        return projects
    matches = [project for project in projects if project.get("id") == project_id]
    if not matches:
        raise SystemExit(f"Unknown project id in scaffold manifest: {project_id}")
    return matches


def _build_output(
    *,
    repo_root: Path,
    projects: list[dict[str, Any]],
    python_major_minor: str | None,
    pdm_version: str | None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for project in projects:
        project_id = project.get("id")
        project_path = project.get("path")
        if not isinstance(project_id, str) or not project_id.strip():
            raise SystemExit(f"Invalid project id in scaffold manifest: {project!r}")
        if not isinstance(project_path, str) or not project_path.strip():
            raise SystemExit(f"Invalid project path in scaffold manifest: {project!r}")
        install_cmd = (
            project.get("tasks", {}).get("install", ["pdm", "install"])
            if isinstance(project.get("tasks"), dict)
            else ["pdm", "install"]
        )
        if not isinstance(install_cmd, list) or not all(isinstance(x, str) for x in install_cmd):
            raise SystemExit(f"Invalid install task for project {project_id!r}: {install_cmd!r}")
        project_dir = repo_root / project_path
        entry = build_install_cache_fingerprint(
            repo_root=repo_root,
            project_dir=project_dir,
            project_id=project_id,
            install_cmd=cast(list[str], install_cmd),
            python_major_minor=python_major_minor,
            pdm_version=pdm_version,
        )
        entries.append(
            {
                "id": entry.project_id,
                "path": entry.project_path,
                "fingerprint": entry.fingerprint,
                "payload": entry.payload,
            }
        )
    return {
        "schema_version": 1,
        "repo_root": str(repo_root),
        "python_major_minor": python_major_minor,
        "pdm_version": pdm_version,
        "projects": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute scaffold install-cache fingerprints.")
    parser.add_argument("--repo-root", type=Path, default=_repo_root())
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--all", action="store_true")
    selector.add_argument("--project", help="Single manifest project id to fingerprint.")
    parser.add_argument("--json", action="store_true", help="Render JSON output (default behavior).")
    parser.add_argument("--python-major-minor")
    parser.add_argument("--pdm-version")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    projects = _select_projects(repo_root=repo_root, project_id=args.project)
    payload = _build_output(
        repo_root=repo_root,
        projects=projects,
        python_major_minor=args.python_major_minor,
        pdm_version=args.pdm_version,
    )
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
