from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if __package__ in (None, ""):
    # Support running as a script:
    #   python tools/monorepo_publish/publish_snapshots.py ...
    _repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_repo_root))

from tools.monorepo_publish.discover import DiscoverError, PythonPackage, discover_python_packages
from tools.monorepo_publish.publisher_python import PublishCommandError, build_dist, twine_upload
from tools.monorepo_publish.rewrite import RewriteError, rewrite_pyproject_for_snapshot
from tools.monorepo_publish.versioning import VersioningError, compute_snapshot_id, snapshot_version


class PublishSnapshotsError(RuntimeError):
    pass


_FORBIDDEN_DIST_BASENAMES = {
    ".env",
    ".netrc",
    ".pypirc",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
_FORBIDDEN_DIST_EXTS = {".key", ".p12", ".pem", ".pfx"}


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists() or (candidate / "packages").exists():
            return candidate
    raise PublishSnapshotsError(
        "Could not auto-detect repo root. Pass --repo-root explicitly (must contain packages/)."
    )


def _read_pyproject(pyproject_path: Path) -> dict:
    try:
        return tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise PublishSnapshotsError(f"Failed to parse {pyproject_path}: {e}") from e


def _iter_requirement_strings(pyproject: dict) -> list[str]:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return []

    out: list[str] = []
    deps = project.get("dependencies")
    if isinstance(deps, list):
        out.extend([str(x) for x in deps])

    opt = project.get("optional-dependencies")
    if isinstance(opt, dict):
        for v in opt.values():
            if isinstance(v, list):
                out.extend([str(x) for x in v])
    return out


def _internal_deps_for_package(
    pkg: PythonPackage,
    *,
    all_packages: dict[str, PythonPackage],
    eligible_versions: dict[str, str],
) -> set[str]:
    pyproject = _read_pyproject(pkg.path / "pyproject.toml")
    internal: set[str] = set()
    for req_s in _iter_requirement_strings(pyproject):
        try:
            req = Requirement(req_s)
        except Exception as e:
            raise PublishSnapshotsError(f"Invalid requirement in {pkg.path}: {req_s!r}: {e}") from e

        dep_key = canonicalize_name(req.name)
        if dep_key == canonicalize_name(pkg.name):
            continue

        if dep_key in all_packages:
            if dep_key not in eligible_versions:
                dep = all_packages[dep_key]
                raise PublishSnapshotsError(
                    f"Eligible package {pkg.name!r} depends on monorepo package {dep.name!r} "
                    f"which is not eligible for publishing (status={dep.status!r}). "
                    "Either mark the dependency package as non-internal, or remove the dependency."
                )
            internal.add(dep_key)

        if req.url is not None and req.url.startswith("file:") and dep_key not in all_packages:
            raise PublishSnapshotsError(
                f"Eligible package {pkg.name!r} has a local-path dependency that is not a monorepo package: "
                f"{req_s!r}. Snapshot publishing cannot include file:// dependencies."
            )

    return internal


def _toposort(nodes: set[str], deps: dict[str, set[str]]) -> list[str]:
    dependents: dict[str, set[str]] = {n: set() for n in nodes}
    indegree: dict[str, int] = {n: 0 for n in nodes}

    for n in nodes:
        for d in deps.get(n, set()):
            if d not in nodes:
                continue
            dependents[d].add(n)
            indegree[n] += 1

    queue = sorted([n for n, deg in indegree.items() if deg == 0])
    out: list[str] = []
    while queue:
        n = queue.pop(0)
        out.append(n)
        for child in sorted(dependents.get(n, set())):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
        queue.sort()

    if len(out) == len(nodes):
        return out

    remaining = nodes - set(out)
    cycle = _find_cycle(remaining, deps)
    if cycle:
        pretty = " -> ".join(cycle + [cycle[0]])
        raise PublishSnapshotsError(f"Internal dependency cycle detected: {pretty}")
    raise PublishSnapshotsError(f"Internal dependency cycle detected among: {sorted(remaining)}")


def _find_cycle(nodes: set[str], deps: dict[str, set[str]]) -> list[str] | None:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        visiting.add(n)
        stack.append(n)
        for d in deps.get(n, set()):
            if d not in nodes:
                continue
            if d in visiting:
                idx = stack.index(d)
                return stack[idx:]
            if d not in visited:
                found = dfs(d)
                if found:
                    return found
        stack.pop()
        visiting.remove(n)
        visited.add(n)
        return None

    for n in sorted(nodes):
        if n in visited:
            continue
        found = dfs(n)
        if found:
            return found
    return None


def _copytree(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for n in names:
            if n in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".venv"}:
                ignored.add(n)
            if n.endswith(".pyc"):
                ignored.add(n)
        return ignored

    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)


def _gitlab_repository_url(base_url: str, project_id: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/api/v4/projects/{project_id}/packages/pypi"


def _run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="monorepo_publish_selftest_") as td:
        root = Path(td)
        pkg_b = root / "pkg_b"
        pkg_a = root / "pkg_a"
        pkg_b.mkdir()
        pkg_a.mkdir()

        (pkg_b / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "pkg_b"',
                    'version = "0.1.0"',
                    "dependencies = []",
                    "",
                    "[build-system]",
                    'requires = ["setuptools>=68"]',
                    'build-backend = "setuptools.build_meta"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (pkg_a / "pyproject.toml").write_text(
            "\n".join(
                [
                    "[project]",
                    'name = "pkg_a"',
                    'version = "0.1.0"',
                    # Use a direct file URL and a hyphenated name to test normalization.
                    'dependencies = ["pkg-b @ file:///${PROJECT_ROOT}/../pkg_b"]',
                    "",
                    "[build-system]",
                    'requires = ["setuptools>=68"]',
                    'build-backend = "setuptools.build_meta"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        snapshot_id = compute_snapshot_id({"MONOREPO_SNAPSHOT_ID": "123"})
        versions = {
            canonicalize_name("pkg_a"): snapshot_version("0.1.0", snapshot_id),
            canonicalize_name("pkg_b"): snapshot_version("0.1.0", snapshot_id),
        }

        rewrite_pyproject_for_snapshot(pkg_b / "pyproject.toml", versions)
        rewrite_pyproject_for_snapshot(pkg_a / "pyproject.toml", versions)

        rewritten_a = tomllib.loads((pkg_a / "pyproject.toml").read_text(encoding="utf-8"))
        rewritten_b = tomllib.loads((pkg_b / "pyproject.toml").read_text(encoding="utf-8"))

        assert rewritten_a["project"]["version"] == versions[canonicalize_name("pkg_a")]
        assert rewritten_b["project"]["version"] == versions[canonicalize_name("pkg_b")]

        deps_a = rewritten_a["project"]["dependencies"]
        assert len(deps_a) == 1
        assert deps_a[0] == f"pkg-b=={versions[canonicalize_name('pkg_b')]}"
        assert "file://" not in deps_a[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish_snapshots",
        description="Publish snapshot builds of eligible monorepo Python packages to a GitLab PyPI registry.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repo root (auto-detected if omitted). Must contain packages/.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute versions and validate rewrites; no publish.")
    parser.add_argument(
        "--confirm-live-publish",
        action="store_true",
        help=(
            "Required to actually upload snapshot builds. Without this flag, the command will "
            "refuse to publish. Use --dry-run to preview."
        ),
    )
    parser.add_argument(
        "--validate-dists",
        action="store_true",
        help=(
            "Build sdists/wheels and validate their contents (no upload). This is intended to be "
            "safe to run in CI on every push."
        ),
    )
    parser.add_argument("--self-test", action="store_true", help="Run local self-test and exit.")
    return parser


def _iter_dist_members(dist_path: Path) -> list[str]:
    name = dist_path.name
    if name.endswith(".whl"):
        with zipfile.ZipFile(dist_path) as zf:
            return [str(n) for n in zf.namelist()]

    if name.endswith(".tar.gz"):
        try:
            with tarfile.open(dist_path, mode="r:gz") as tf:
                return [m.name for m in tf.getmembers() if isinstance(m.name, str)]
        except tarfile.TarError as e:
            raise PublishSnapshotsError(f"Failed to read sdist archive {dist_path}: {e}") from e

    raise PublishSnapshotsError(f"Unsupported distribution type for validation: {dist_path}")


def _is_forbidden_dist_member(member_path: str) -> bool:
    normalized = member_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    lower = normalized.lower()
    if lower.endswith("/"):
        return False
    base = lower.rsplit("/", 1)[-1]
    if base in _FORBIDDEN_DIST_BASENAMES:
        return True
    return any(base.endswith(ext) for ext in _FORBIDDEN_DIST_EXTS)


def _validate_dist_file_contents(dist_path: Path) -> None:
    forbidden: list[str] = []
    for member in _iter_dist_members(dist_path):
        if _is_forbidden_dist_member(member):
            forbidden.append(member)

    if not forbidden:
        return

    sample = forbidden[:25]
    extra = f" (+{len(forbidden) - len(sample)} more)" if len(forbidden) > len(sample) else ""
    pretty = "\n".join(f"- {p}" for p in sample)
    raise PublishSnapshotsError(
        "Built distribution contains forbidden file(s); refusing to continue.\n"
        f"dist={dist_path.name}\n"
        f"forbidden_members:\n{pretty}{extra}\n"
        "Tip: remove the file(s) from the package source tree or adjust packaging excludes so they "
        "do not land in sdists/wheels."
    )


def _validate_dist_dir(dist_dir: Path) -> None:
    dists = sorted([p for p in dist_dir.iterdir() if p.is_file()])
    if not dists:
        raise PublishSnapshotsError(f"No built distributions found to validate in: {dist_dir}")

    for dist in dists:
        _validate_dist_file_contents(dist)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test:
        _run_self_test()
        snapshot_id = compute_snapshot_id(os.environ)
        print(f"self-test: ok (snapshot_id={snapshot_id})")
        return 0

    if bool(args.dry_run) and bool(args.validate_dists):
        raise PublishSnapshotsError("Choose one: --dry-run or --validate-dists (they are mutually exclusive).")

    repo_root = args.repo_root.resolve() if args.repo_root is not None else _find_repo_root(Path.cwd())
    packages = discover_python_packages(repo_root)

    all_packages: dict[str, PythonPackage] = {}
    for pkg in packages:
        key = canonicalize_name(pkg.name)
        if key in all_packages:
            raise PublishSnapshotsError(
                f"Duplicate package name after normalization: {pkg.name!r} conflicts with {all_packages[key].name!r}"
            )
        all_packages[key] = pkg

    eligible = [p for p in packages if p.status != "internal"]
    if not eligible:
        print("eligible packages (0): (none)")
        return 0

    snapshot_id = compute_snapshot_id(os.environ)
    eligible_versions = {
        canonicalize_name(p.name): snapshot_version(p.base_version, snapshot_id) for p in eligible
    }

    deps: dict[str, set[str]] = {}
    for pkg in eligible:
        deps[canonicalize_name(pkg.name)] = _internal_deps_for_package(
            pkg, all_packages=all_packages, eligible_versions=eligible_versions
        )

    order = _toposort(set(eligible_versions.keys()), deps)

    print(f"snapshot_id: {snapshot_id}")
    print(f"eligible packages ({len(order)}):")
    for key in order:
        pkg = all_packages[key]
        print(f"  - {pkg.name} ({pkg.status}) base={pkg.base_version} snapshot={eligible_versions[key]}")

    if not args.dry_run and not args.validate_dists and not args.confirm_live_publish:
        raise PublishSnapshotsError(
            "Refusing to publish snapshots without explicit confirmation. "
            "Pass --confirm-live-publish to upload, or --dry-run/--validate-dists for safe CI checks."
        )

    with tempfile.TemporaryDirectory(prefix="monorepo_publish_") as td:
        temp_root = Path(td)
        temp_packages: dict[str, Path] = {}
        for key in order:
            src = all_packages[key].path
            rel = src.relative_to(repo_root)
            dst = temp_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copytree(src, dst)
            rewrite_pyproject_for_snapshot(dst / "pyproject.toml", eligible_versions)
            temp_packages[key] = dst

        if args.dry_run:
            print("")
            print("dry-run: validated rewrites; would publish:")
            for key in order:
                print(f"  - {all_packages[key].name}=={eligible_versions[key]}")
            return 0

        if args.validate_dists:
            validated: list[str] = []
            for key in order:
                pkg = all_packages[key]
                pkg_dir = temp_packages[key]
                print("")
                print(f"validating: {pkg.name}=={eligible_versions[key]}")
                dist_dir = build_dist(pkg_dir)
                _validate_dist_dir(dist_dir)
                validated.append(f"{pkg.name}=={eligible_versions[key]}")

            print("")
            print("validate-dists: ok")
            for line in validated:
                print(f"  - {line}")
            return 0

        base_url = os.environ.get("GITLAB_BASE_URL") or "https://gitlab.com"
        project_id = os.environ.get("GITLAB_PYPI_PROJECT_ID")
        username = os.environ.get("GITLAB_PYPI_USERNAME")
        password = os.environ.get("GITLAB_PYPI_PASSWORD")
        if not project_id or not username or not password:
            raise PublishSnapshotsError(
                "Missing GitLab publishing env vars. Required: "
                "GITLAB_PYPI_PROJECT_ID, GITLAB_PYPI_USERNAME, GITLAB_PYPI_PASSWORD "
                "(and optionally GITLAB_BASE_URL)."
            )

        repository_url = _gitlab_repository_url(base_url, project_id)

        published: dict[str, str] = {}
        for key in order:
            pkg = all_packages[key]
            pkg_dir = temp_packages[key]
            print("")
            print(f"publishing: {pkg.name}=={eligible_versions[key]}")
            dist_dir = build_dist(pkg_dir)
            _validate_dist_dir(dist_dir)
            twine_upload(dist_dir, repository_url, username=username, password=password)
            published[pkg.name] = eligible_versions[key]

        print("")
        print("published:")
        for name in sorted(published):
            print(f"  {name}=={published[name]}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DiscoverError, VersioningError, RewriteError, PublishCommandError, PublishSnapshotsError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
