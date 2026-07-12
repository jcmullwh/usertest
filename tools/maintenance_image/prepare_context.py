#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Python 3.11+ is required for prepare_context.py.") from exc

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("prepare_context.py requires PyYAML.") from exc


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SystemExit(f"Expected YAML mapping in {path}, got {type(raw).__name__}")
    return raw


def _load_manifest_projects(repo_root: Path) -> list[dict[str, Any]]:
    manifest_path = repo_root / "tools" / "scaffold" / "monorepo.toml"
    raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    projects = raw.get("projects", [])
    if not isinstance(projects, list):
        raise SystemExit(f"Invalid scaffold manifest: expected [[projects]] in {manifest_path}")
    normalized: list[dict[str, Any]] = []
    for project in projects:
        if not isinstance(project, dict):
            raise SystemExit(f"Invalid scaffold project entry in {manifest_path}: {project!r}")
        normalized.append(project)
    return normalized


def _merge_unique(items: list[str], extra: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in [*items, *extra]:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _write_manifest(path: Path, *, header: str, items: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [header, "#", "# Generated for maintenance profile builds.", ""]
    if items:
        lines.extend(items)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _ignore_transient_python_artifacts(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "__pycache__" or Path(name).suffix.casefold() in {".pyc", ".pyo"}
    }


def _copy_tree(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(
            src,
            dest,
            dirs_exist_ok=True,
            ignore=_ignore_transient_python_artifacts,
        )
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _copy_repo_snapshot(*, repo_root: Path, output_dir: Path) -> list[str]:
    snapshot_root = output_dir / "repo_snapshot"
    copied: list[str] = []

    for rel in (
        Path("requirements-dev.txt"),
        Path("configs"),
        Path("tools/scaffold"),
        Path("scripts/smoke.sh"),
        Path("scripts/smoke.ps1"),
    ):
        src = repo_root / rel
        if not src.exists():
            continue
        _copy_tree(src, snapshot_root / rel)
        copied.append(rel.as_posix())

    for project in _load_manifest_projects(repo_root):
        raw_path = project.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise SystemExit(f"Invalid project path in scaffold manifest: {project!r}")
        project_rel = Path(raw_path)
        for rel in (
            project_rel / "pyproject.toml",
            project_rel / "pdm.lock",
            project_rel / "README.md",
            project_rel / "src",
        ):
            src = repo_root / rel
            if not src.exists():
                continue
            _copy_tree(src, snapshot_root / rel)
            copied.append(rel.as_posix())
    return sorted(set(copied))


def _collect_agent_install_union(repo_root: Path) -> dict[str, list[str]]:
    agents = _load_yaml_mapping(repo_root / "configs" / "agents.yaml").get("agents", {})
    if not isinstance(agents, dict):
        raise SystemExit("configs/agents.yaml must contain an 'agents' mapping")
    merged = {"apt": [], "pip": [], "npm_global": []}
    for agent_name in ("codex", "claude", "gemini"):
        raw_agent = agents.get(agent_name)
        if not isinstance(raw_agent, dict):
            raise SystemExit(f"Missing or invalid agents.{agent_name} in configs/agents.yaml")
        install = raw_agent.get("sandbox_cli_install", {})
        if not isinstance(install, dict):
            raise SystemExit(
                f"Missing or invalid agents.{agent_name}.sandbox_cli_install in configs/agents.yaml"
            )
        merged["apt"] = _merge_unique(merged["apt"], _coerce_str_list(install.get("apt")))
        merged["pip"] = _merge_unique(merged["pip"], _coerce_str_list(install.get("pip")))
        merged["npm_global"] = _merge_unique(
            merged["npm_global"], _coerce_str_list(install.get("npm_global"))
        )
    return merged


def _coerce_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


_FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)", re.IGNORECASE)


def _python_major_minor_from_dockerfile(dockerfile: Path) -> str:
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = _FROM_RE.match(line)
        if not match:
            continue
        image = match.group("image")
        tag = image.rsplit(":", maxsplit=1)[-1]
        version = tag.split("-", maxsplit=1)[0]
        parts = version.split(".")
        if len(parts) >= 2 and all(part.isdigit() for part in parts[:2]):
            return f"{parts[0]}.{parts[1]}"
    raise SystemExit(f"Unable to infer Python major.minor from Dockerfile: {dockerfile}")


def _pdm_version_from_manifest(pip_manifest: Path) -> str:
    for line in pip_manifest.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("pdm=="):
            return stripped.split("==", maxsplit=1)[1]
    raise SystemExit(f"Unable to infer pinned pdm version from {pip_manifest}")


def prepare_context(*, repo_root: Path, output_dir: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    base_context = (
        repo_root
        / "packages"
        / "sandbox_runner"
        / "src"
        / "sandbox_runner"
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli"
    )
    maintenance_template = (
        repo_root
        / "packages"
        / "sandbox_runner"
        / "src"
        / "sandbox_runner"
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli_maintenance"
    )
    if not base_context.is_dir():
        raise SystemExit(f"Missing base sandbox_cli context: {base_context}")
    if not maintenance_template.is_dir():
        raise SystemExit(f"Missing maintenance context template: {maintenance_template}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(
        base_context,
        output_dir,
        ignore=_ignore_transient_python_artifacts,
    )
    shutil.copytree(
        maintenance_template,
        output_dir,
        dirs_exist_ok=True,
        ignore=_ignore_transient_python_artifacts,
    )

    install_union = _collect_agent_install_union(repo_root)
    manifests_dir = output_dir / "overlays" / "manifests"
    _write_manifest(
        manifests_dir / "apt.txt",
        header="# Maintenance overlay APT packages for all supported agent CLIs.",
        items=install_union["apt"],
    )
    _write_manifest(
        manifests_dir / "pip.txt",
        header="# Maintenance overlay pip packages for all supported agent CLIs.",
        items=install_union["pip"],
    )
    _write_manifest(
        manifests_dir / "npm-global.txt",
        header="# Maintenance overlay global npm packages for all supported agent CLIs.",
        items=install_union["npm_global"],
    )

    copied_paths = _copy_repo_snapshot(repo_root=repo_root, output_dir=output_dir)
    dockerfile = output_dir / "Dockerfile"
    pip_manifest = output_dir / "manifests" / "pip.txt"
    metadata = {
        "schema_version": 1,
        "repo_root": str(repo_root),
        "output_dir": str(output_dir),
        "copied_paths": copied_paths,
        "agent_install_union": install_union,
        "python_major_minor": _python_major_minor_from_dockerfile(dockerfile),
        "pdm_version": _pdm_version_from_manifest(pip_manifest),
    }
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the maintenance Docker build context.")
    parser.add_argument("--repo-root", type=Path, default=_repo_root())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--metadata-out", type=Path)
    args = parser.parse_args(argv)

    metadata = prepare_context(repo_root=args.repo_root, output_dir=args.output_dir)
    rendered = json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
    if args.metadata_out is not None:
        args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
        args.metadata_out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
