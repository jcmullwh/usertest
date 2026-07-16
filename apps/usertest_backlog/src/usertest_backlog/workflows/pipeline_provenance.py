"""Repository-owned source and configuration provenance for backlog runs.

The backlog application is a monorepo composition rather than one import tree.
Its runtime can load sibling applications, any first-party package, repository
CLI shims, runner catalog/configuration, and maintenance-image assets.  Keep the
enumeration and runtime-origin interpretation in one module so release sealing
and ticket-export sealing cannot silently describe different implementations.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import tomllib
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

_IGNORED_CACHE_PARTS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})
_IGNORED_COMPILED_SUFFIXES = frozenset({".pyc", ".pyo"})


def _eligible_file(path: Path) -> bool:
    return (
        path.is_file()
        and not any(part in _IGNORED_CACHE_PARTS for part in path.parts)
        and path.suffix.casefold() not in _IGNORED_COMPILED_SUFFIXES
    )


def _source_directories(source_root: Path) -> list[Path]:
    """Discover every application/library source tree used by the checkout runtime."""

    roots: list[Path] = []
    for container in (source_root / "apps", source_root / "packages"):
        if not container.is_dir():
            continue
        roots.extend(
            component / "src"
            for component in sorted(container.iterdir(), key=lambda item: item.as_posix())
            if component.is_dir() and (component / "src").is_dir()
        )
    return roots


def _top_level_import_names(source_dirs: list[Path]) -> set[str]:
    names: set[str] = set()
    for source_dir in source_dirs:
        for candidate in source_dir.iterdir():
            if candidate.is_dir() and (candidate / "__init__.py").is_file():
                names.add(candidate.name)
            elif candidate.is_file() and candidate.suffix.casefold() == ".py":
                names.add(candidate.stem)
    return names


def _add_tree(candidates: set[Path], root: Path) -> None:
    if not root.is_dir():
        return
    candidates.update(path.resolve() for path in root.rglob("*") if _eligible_file(path))


def _git_metadata_paths(source_root: Path) -> set[Path]:
    """Return mutable Git identity files used by export projection sealing."""

    candidates: set[Path] = set()
    dot_git = source_root / ".git"
    git_dir: Path | None = None
    if dot_git.is_dir():
        git_dir = dot_git.resolve()
    elif dot_git.is_file():
        try:
            marker = dot_git.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            marker = ""
        if marker.casefold().startswith("gitdir:"):
            raw_git_dir = marker.split(":", 1)[1].strip()
            candidate = Path(raw_git_dir)
            if not candidate.is_absolute():
                candidate = source_root / candidate
            git_dir = candidate.resolve()
        candidates.add(dot_git.resolve())
    if git_dir is None:
        return candidates

    candidates.update(
        path.resolve() for path in (git_dir / "HEAD", git_dir / "commondir") if path.is_file()
    )
    common_dir = git_dir
    commondir_path = git_dir / "commondir"
    if commondir_path.is_file():
        try:
            raw_common = commondir_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raw_common = ""
        if raw_common:
            candidate = Path(raw_common)
            if not candidate.is_absolute():
                candidate = git_dir / candidate
            common_dir = candidate.resolve()
    head_path = git_dir / "HEAD"
    if head_path.is_file():
        try:
            head_text = head_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            head_text = ""
        if head_text.startswith("ref:"):
            ref_path = common_dir / head_text.removeprefix("ref:").strip()
            if ref_path.is_file():
                candidates.add(ref_path.resolve())
            else:
                packed_refs = common_dir / "packed-refs"
                if packed_refs.is_file():
                    candidates.add(packed_refs.resolve())
    return candidates


def pipeline_source_config_bindings(
    *,
    source_root: Path,
    config_root: Path | None = None,
    include_git_metadata: bool = True,
) -> dict[str, Path]:
    """Manifest the complete repository-owned executable/configuration surface.

    Discovery intentionally follows the monorepo layout instead of a package-name
    allowlist.  This keeps newly introduced first-party packages inside the seal
    without requiring a second hand-maintained update.
    """

    source_root = source_root.resolve()
    config_root = (config_root or source_root / "configs").resolve()
    candidates: set[Path] = set()
    source_dirs = _source_directories(source_root)
    for source_dir in source_dirs:
        _add_tree(candidates, source_dir)

    # Repository package shims are executable import code. Discover them from
    # the top-level names exposed by app/package source trees.
    for import_name in sorted(_top_level_import_names(source_dirs)):
        shim_root = source_root / import_name
        if (shim_root / "__init__.py").is_file():
            _add_tree(candidates, shim_root)

    _add_tree(candidates, config_root)

    # runner_core builds/loads the maintenance environment through these trees.
    # The sandbox Docker contexts themselves are already covered by package src.
    _add_tree(candidates, source_root / "tools" / "maintenance_image")
    _add_tree(candidates, source_root / "tools" / "scaffold")
    for relative in (
        Path("requirements-dev.txt"),
        Path("scripts") / "smoke.ps1",
        Path("scripts") / "smoke.sh",
    ):
        candidate = source_root / relative
        if _eligible_file(candidate):
            candidates.add(candidate.resolve())

    component_roots = {source_root, *(source_dir.parent for source_dir in source_dirs)}
    for component_root in component_roots:
        for filename in ("pyproject.toml", "pdm.lock", "README.md"):
            candidate = component_root / filename
            if candidate.is_file():
                candidates.add(candidate.resolve())

    if include_git_metadata:
        candidates.update(_git_metadata_paths(source_root))

    bindings: dict[str, Path] = {}
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(source_root).as_posix()
        except ValueError:
            relative = f"external/{sha256(str(path).encode('utf-8')).hexdigest()}/{path.name}"
        bindings[f"pipeline.manifest:{relative}"] = path
    return bindings


def _local_components(source_root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Index monorepo components by normalized distribution and import name."""

    by_distribution: dict[str, Path] = {}
    by_import: dict[str, Path] = {}
    for container in (source_root / "apps", source_root / "packages"):
        if not container.is_dir():
            continue
        for component in sorted(container.iterdir(), key=lambda item: item.as_posix()):
            pyproject = component / "pyproject.toml"
            source_dir = component / "src"
            if not pyproject.is_file() or not source_dir.is_dir():
                continue
            try:
                document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                continue
            project = document.get("project")
            project = project if isinstance(project, Mapping) else {}
            distribution = project.get("name")
            if isinstance(distribution, str) and distribution.strip():
                by_distribution[distribution.strip().casefold().replace("-", "_")] = component
            for import_name in _top_level_import_names([source_dir]):
                by_import[import_name] = component
    return by_distribution, by_import


def _declared_local_dependencies(
    component: Path,
    *,
    by_distribution: Mapping[str, Path],
) -> set[Path]:
    try:
        document = tomllib.loads((component / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    project = document.get("project")
    project = project if isinstance(project, Mapping) else {}
    dependencies = project.get("dependencies")
    dependencies = dependencies if isinstance(dependencies, list) else []
    result: set[Path] = set()
    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue
        # PEP 508 distribution names end before extras, a version operator,
        # whitespace, or a direct-reference marker.  Local projects are indexed
        # with hyphen/underscore normalization as packaging tools do.
        name = dependency.strip()
        for separator in ("[", " ", "@", "<", ">", "=", "!", "~", ";"):
            name = name.split(separator, 1)[0]
        local = by_distribution.get(name.casefold().replace("-", "_"))
        if local is not None:
            result.add(local)
    return result


def _imported_local_dependencies(
    component: Path,
    *,
    by_import: Mapping[str, Path],
) -> set[Path]:
    result: set[Path] = set()
    source_dir = component / "src"
    for path in source_dir.rglob("*.py"):
        if not _eligible_file(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names.extend(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.partition(".")[0])
            for name in names:
                local = by_import.get(name)
                if local is not None and local != component:
                    result.add(local)
    return result


def pipeline_runtime_compatibility_bindings(
    *,
    source_root: Path,
    config_root: Path | None = None,
) -> dict[str, Path]:
    """Return the behavior-affecting backlog-runtime compatibility surface.

    The full qualification seal deliberately captures the entire checkout for
    forensic reproducibility.  Stability handoff needs a separate projection:
    recursively follow the backlog application's declared and imported local
    dependencies, then add the configuration and tool inputs that those runtime
    paths consume.  This avoids treating unrelated target/implementation apps as
    backlog-runtime changes without relying on a benchmark-specific package list.
    """

    source_root = source_root.resolve()
    config_root = (config_root or source_root / "configs").resolve()
    by_distribution, by_import = _local_components(source_root)
    root_component = by_distribution.get("usertest_backlog")
    if root_component is None:
        conventional_root = source_root / "apps" / "usertest_backlog"
        root_component = conventional_root if (conventional_root / "src").is_dir() else None
    if root_component is None:
        return {}

    components: set[Path] = set()
    pending = [root_component]
    while pending:
        component = pending.pop()
        if component in components:
            continue
        components.add(component)
        dependencies = _declared_local_dependencies(
            component,
            by_distribution=by_distribution,
        ) | _imported_local_dependencies(component, by_import=by_import)
        pending.extend(sorted(dependencies - components, key=lambda item: item.as_posix()))

    candidates: set[Path] = set()
    included_import_names: set[str] = set()
    for component in components:
        source_dir = component / "src"
        _add_tree(candidates, source_dir)
        included_import_names.update(_top_level_import_names([source_dir]))
        for filename in ("pyproject.toml", "pdm.lock", "README.md"):
            candidate = component / filename
            if _eligible_file(candidate):
                candidates.add(candidate.resolve())

    for import_name in sorted(included_import_names):
        shim_root = source_root / import_name
        if (shim_root / "__init__.py").is_file():
            _add_tree(candidates, shim_root)

    _add_tree(candidates, config_root)
    _add_tree(candidates, source_root / "tools" / "maintenance_image")
    _add_tree(candidates, source_root / "tools" / "scaffold")
    for relative in (
        Path("requirements-dev.txt"),
        Path("scripts") / "smoke.ps1",
        Path("scripts") / "smoke.sh",
    ):
        candidate = source_root / relative
        if _eligible_file(candidate):
            candidates.add(candidate.resolve())

    bindings: dict[str, Path] = {}
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        relative = path.relative_to(source_root).as_posix()
        bindings[f"pipeline.runtime_compatibility:{relative}"] = path
    return bindings


def _sealed_python_contract(
    *,
    repo_root: Path,
    pipeline_manifest: Mapping[str, Any],
) -> tuple[set[Path], set[str], list[str]]:
    files_raw = pipeline_manifest.get("files")
    files = files_raw if isinstance(files_raw, list) else []
    sealed_paths: set[Path] = set()
    import_names: set[str] = set()
    errors: list[str] = []
    if not isinstance(files_raw, list):
        return sealed_paths, import_names, ["pipeline_manifest_files_invalid"]

    for index, receipt in enumerate(files):
        if not isinstance(receipt, Mapping):
            errors.append(f"pipeline_manifest_file_receipt_invalid:{index}")
            continue
        relative_raw = receipt.get("path")
        if not isinstance(relative_raw, str) or not relative_raw.strip():
            errors.append(f"pipeline_manifest_file_path_invalid:{index}")
            continue
        relative = PurePosixPath(relative_raw)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"pipeline_manifest_file_path_unsafe:{index}")
            continue
        candidate = (repo_root / Path(*relative.parts)).resolve()
        try:
            candidate.relative_to(repo_root)
        except ValueError:
            errors.append(f"pipeline_manifest_file_path_outside_root:{index}")
            continue
        sealed_paths.add(candidate)

        parts = relative.parts
        if len(parts) >= 4 and parts[0] in {"apps", "packages"} and parts[2] == "src":
            import_part = parts[3]
            import_name = PurePosixPath(import_part).stem
            if import_name.isidentifier():
                import_names.add(import_name)
        elif len(parts) >= 2 and parts[1] == "__init__.py" and parts[0].isidentifier():
            import_names.add(parts[0])
    return sealed_paths, import_names, errors


def _source_path_for_loaded_module(path: Path) -> Path:
    if path.suffix.casefold() not in _IGNORED_COMPILED_SUFFIXES:
        return path
    try:
        return Path(importlib.util.source_from_cache(str(path))).resolve()
    except (NotImplementedError, ValueError):
        return path


def first_party_module_binding_errors(
    *,
    modules: Mapping[str, Any],
    repo_root: Path,
    pipeline_manifest: Mapping[str, Any],
) -> list[str]:
    """Reject first-party imports whose exact source file is absent from the seal."""

    repo_root = repo_root.resolve()
    sealed_paths, import_names, errors = _sealed_python_contract(
        repo_root=repo_root,
        pipeline_manifest=pipeline_manifest,
    )
    for module_name, module in sorted(modules.items()):
        top_level = module_name.partition(".")[0]
        if top_level not in import_names:
            continue
        module_path_raw = getattr(module, "__file__", None)
        if not isinstance(module_path_raw, (str, os.PathLike)):
            continue
        module_path = _source_path_for_loaded_module(Path(module_path_raw).resolve())
        if module_path not in sealed_paths:
            errors.append(f"first_party_module_not_sealed:{module_name}:{module_path}")
    return list(dict.fromkeys(errors))


__all__ = [
    "first_party_module_binding_errors",
    "pipeline_runtime_compatibility_bindings",
    "pipeline_source_config_bindings",
]
