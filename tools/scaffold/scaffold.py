"""
Scaffold: monorepo project management CLI.

This script is intentionally stdlib-only. It shells out to external tools only when a chosen generator or task needs
them.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


class ScaffoldError(RuntimeError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _registry_path(repo_root: Path) -> Path:
    return repo_root / "tools" / "scaffold" / "registry.toml"


def _manifest_path(repo_root: Path) -> Path:
    return repo_root / "tools" / "scaffold" / "monorepo.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ScaffoldError(f"Missing TOML file: {path}")
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:  # pragma: no cover
        try:
            import tomli
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ScaffoldError(
                "TOML parsing requires Python 3.11+ (tomllib) or an installed 'tomli' package."
            ) from exc
        data = tomli.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ScaffoldError(f"Invalid TOML root in {path}: expected table")
    return data


def _toml_quote_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n").replace('"', '\\"')
    )
    return f'"{escaped}"'


_TOML_BARE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MANIFEST_SCHEMA_VERSION = 1


def _toml_format_key(key: str) -> str:
    """Format a TOML key, quoting it only when required."""
    if _TOML_BARE_KEY_RE.match(key):
        return key
    return _toml_quote_string(key)


def _toml_format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, _dt.time):
        return value.isoformat()
    if isinstance(value, str):
        return _toml_quote_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_format_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner_parts: list[str] = []
        for k, v in value.items():
            if not isinstance(k, str) or not k:
                raise ScaffoldError(f"Unsupported TOML dict key type: {type(k).__name__}")
            inner_parts.append(f"{_toml_format_key(k)} = {_toml_format_value(v)}")
        inner = ", ".join(inner_parts)
        return "{ " + inner + " }"
    raise ScaffoldError(f"Unsupported TOML value type: {type(value).__name__}")


def _load_manifest(repo_root: Path) -> dict[str, Any]:
    """Load the full manifest dict and validate/normalize `schema_version`."""
    manifest_path = _manifest_path(repo_root)
    if not manifest_path.exists():
        return {"schema_version": _MANIFEST_SCHEMA_VERSION, "projects": []}

    data = _load_toml(manifest_path)
    schema_version = data.get("schema_version")
    if schema_version is None:
        data["schema_version"] = _MANIFEST_SCHEMA_VERSION
    else:
        if not isinstance(schema_version, int):
            raise ScaffoldError("monorepo.toml: schema_version must be an integer")
        if schema_version > _MANIFEST_SCHEMA_VERSION:
            raise ScaffoldError(
                f"monorepo.toml: schema_version {schema_version} is newer than this scaffold tool supports "
                f"({_MANIFEST_SCHEMA_VERSION})"
            )
    return data


def _load_projects(repo_root: Path) -> list[dict[str, Any]]:
    data = _load_manifest(repo_root)
    projects = data.get("projects", [])
    if projects is None:
        return []
    if not isinstance(projects, list):
        raise ScaffoldError("monorepo.toml: expected [[projects]] array")
    for project in projects:
        if not isinstance(project, dict):
            raise ScaffoldError("monorepo.toml: each [[projects]] entry must be a table")
    return cast(list[dict[str, Any]], projects)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# This file is managed by tools/scaffold/scaffold.py.")
    lines.append("# It is the source of truth for what projects exist in this monorepo.")
    lines.append("")

    schema_version = manifest.get("schema_version", _MANIFEST_SCHEMA_VERSION)
    if not isinstance(schema_version, int):
        raise ScaffoldError("monorepo.toml: schema_version must be an integer")
    lines.append(f"schema_version = {schema_version}")
    lines.append("")

    extra_keys = [k for k in manifest.keys() if k not in {"schema_version", "projects"}]
    for key in sorted(extra_keys):
        lines.append(f"{_toml_format_key(key)} = {_toml_format_value(manifest[key])}")
    if extra_keys:
        lines.append("")

    projects = manifest.get("projects", [])
    if projects is None:
        projects = []
    if not isinstance(projects, list):
        raise ScaffoldError("monorepo.toml: expected [[projects]] array")

    for project in projects:
        if not isinstance(project, dict):
            raise ScaffoldError("monorepo.toml: each [[projects]] entry must be a table")
        lines.append("[[projects]]")

        emitted: set[str] = set()
        for key in ("id", "kind", "path", "generator", "toolchain", "package_manager"):
            if key in project:
                lines.append(f"{_toml_format_key(key)} = {_toml_format_value(project[key])}")
                emitted.add(key)

        for key in (
            "generator_source",
            "generator_ref",
            "generator_resolved_commit",
            "generator_pinned",
        ):
            if key in project:
                lines.append(f"{_toml_format_key(key)} = {_toml_format_value(project[key])}")
                emitted.add(key)

        for key in sorted(project.keys()):
            if key in emitted or key in {"ci", "tasks"}:
                continue
            lines.append(f"{_toml_format_key(key)} = {_toml_format_value(project[key])}")

        ci = project.get("ci")
        if isinstance(ci, dict):
            lines.append(f"ci = {_toml_format_value(ci)}")
        elif ci is not None:
            raise ScaffoldError("monorepo.toml: projects[].ci must be a table when present")

        tasks = project.get("tasks")
        if isinstance(tasks, dict):
            for task_name in sorted(tasks.keys(), key=str):
                if not isinstance(task_name, str) or not task_name:
                    raise ScaffoldError("monorepo.toml: projects[].tasks must use non-empty string task names")
                _validate_task_name(task_name, where="monorepo.toml: projects[].tasks")
                lines.append(f"tasks.{task_name} = {_toml_format_value(tasks[task_name])}")
        elif tasks is not None:
            raise ScaffoldError("monorepo.toml: projects[].tasks must be a table when present")

        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _pdm_importable() -> bool:
    return importlib.util.find_spec("pdm") is not None


def _resolve_argv(argv: list[str]) -> list[str]:
    """Resolve argv[0] via PATH for cross-platform execution.

    On Windows, common toolchain entrypoints (notably `npm`) are often `.cmd` shims. `subprocess.run()` cannot execute
    `.cmd`/`.bat` files directly, so we invoke them via `cmd.exe /c`.
    """

    if not argv:
        raise ScaffoldError("Internal error: empty argv")

    cmd = argv[0]

    # Windows-only: `pdm` can hang at import time on hosts where stdlib `platform.system()` hangs due to WMI queries.
    # We route pdm invocations through a shim that disables WMI-backed platform queries.
    if os.name == "nt":
        cmd_name = Path(cmd).name.lower()
        if cmd_name in {"pdm", "pdm.exe", "pdm.cmd", "pdm.bat"}:
            shim = _repo_root() / "tools" / "pdm_shim.py"
            if not shim.exists():
                raise ScaffoldError(f"Missing PDM shim: {shim}")
            # Prefer running PDM in the same interpreter as this scaffold process. This avoids relying on `python`
            # being on PATH and avoids mixing tool installations across interpreters.
            if _pdm_importable():
                return [sys.executable, str(shim), *argv[1:]]
            # Fall back to invoking `pdm` directly. This keeps `scaffold` usable when PDM is installed in a different
            # interpreter than the one running scaffold.

    if any(sep and sep in cmd for sep in ("/", "\\", os.path.sep, os.path.altsep)):
        return argv

    resolved = _which(cmd)
    if resolved is None:
        return argv

    if os.name == "nt":
        suffix = Path(resolved).suffix.lower()
        if suffix in {".cmd", ".bat"}:
            comspec = os.environ.get("ComSpec", "cmd.exe")
            return [comspec, "/d", "/c", resolved, *argv[1:]]

    return [resolved, *argv[1:]]


def _require_on_path(cmd: str, *, why: str) -> None:
    if _which(cmd) is None:
        raise ScaffoldError(f"Required command not found on PATH: {cmd} ({why})")


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    resolved_argv = _resolve_argv(argv)
    _eprint(f"+ ({cwd}) {' '.join(argv)}")
    if resolved_argv != argv:
        _eprint(f"  -> ({cwd}) {' '.join(resolved_argv)}")
    try:
        return subprocess.run(
            resolved_argv,
            cwd=str(cwd),
            env=env,
            text=True,
            check=False,
            capture_output=capture,
        )
    except FileNotFoundError as exc:
        cmd_name = Path(argv[0]).name
        hint = ""
        if cmd_name.lower() in {"pdm", "pdm.exe", "pdm.cmd", "pdm.bat"}:
            hint = f" Install PDM: {sys.executable} -m pip install -U pdm"
        raise ScaffoldError(f"Command not found: {cmd_name!r}.{hint}") from exc
    except OSError as exc:
        raise ScaffoldError(f"Failed to execute {argv[0]!r}: {exc}") from exc


def _probe_tool_version(*, argv: list[str], timeout_seconds: float) -> tuple[bool, str | None, str | None]:
    """
    Best-effort `--version` probe for a tool.

    Returns
    -------
    tuple[bool, str | None, str | None]
        `(ok, version_line, error)`.
    """
    try:
        cp = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, None, f"timed out after {timeout_seconds:.1f}s"
    except OSError as exc:
        return False, None, str(exc)

    combined = "\n".join(x for x in (cp.stdout, cp.stderr) if x).strip()
    line = combined.splitlines()[0].strip() if combined else None
    if cp.returncode != 0:
        return False, line, f"exit_code={cp.returncode}"
    return True, line, None


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _probe_temp_writable(*, timeout_seconds: float) -> tuple[bool, str | None, str | None]:
    del timeout_seconds  # reserved for future parity with other probes
    tmp_dir = Path(tempfile.gettempdir())
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="scaffold_doctor_", dir=str(tmp_dir))
        try:
            os.write(fd, b"ok")
        finally:
            os.close(fd)
        os.unlink(name)
    except OSError as exc:
        return False, str(tmp_dir), str(exc)
    return True, str(tmp_dir), None


def _pip_remediation_hint(*, python_exe: str) -> str:
    parts: list[str] = []
    parts.append(f"Try: {python_exe} -m ensurepip --upgrade")
    parts.append(f"Then: {python_exe} -m pip --version")
    parts.append("If ensurepip is missing, install a full CPython (python.org) with pip included.")
    return " ".join(parts)


def _git_remediation_hint() -> str:
    if os.name == "nt":
        return "Install Git and ensure 'git' is on PATH (for example: winget install -e --id Git.Git)."
    return "Install Git and ensure 'git' is on PATH."


def _bash_remediation_hint() -> str:
    if os.name == "nt":
        return "Install a bash (Git for Windows or WSL) or use PowerShell-only workflows."
    return "Install bash (required for scripts/smoke.sh) and ensure it is on PATH."


def _write_doctor_tool_report(*, repo_root: Path, payload: dict[str, Any]) -> Path | None:
    out_path = repo_root / ".scaffold" / "doctor_tool_report.json"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return out_path
    except OSError as exc:
        _eprint(f"WARNING: failed to write doctor tool report: {out_path}: {exc}")
        return None


_KNOWN_TRANSIENT_PDM_LOCAL_PATH_MARKERS: tuple[str, ...] = (
    "unable to find candidates",
    "no candidate is found",
    "no matching distribution found",
    "resolutionimpossible",
    "none of the providers can be satisfied",
    "filenotfounderror",
    "no such file or directory",
)


def _is_pdm_install_command(argv: list[str]) -> bool:
    if len(argv) < 2:
        return False
    cmd_name = Path(argv[0]).name.lower()
    if cmd_name not in {"pdm", "pdm.exe", "pdm.cmd", "pdm.bat"}:
        return False
    return argv[1].strip().lower() == "install"


def _is_pdm_command(argv: list[str]) -> bool:
    if not argv:
        return False
    cmd_name = Path(argv[0]).name.lower()
    return cmd_name in {"pdm", "pdm.exe", "pdm.cmd", "pdm.bat"}


def _looks_like_transient_pdm_local_path_failure(*, stdout: str, stderr: str) -> bool:
    text = "\n".join(x for x in (stdout, stderr) if x).lower()
    if not text:
        return False
    if "normalized-events" not in text and "normalized_events" not in text:
        return False
    return any(marker in text for marker in _KNOWN_TRANSIENT_PDM_LOCAL_PATH_MARKERS)


def _emit_captured_process_output(cp: subprocess.CompletedProcess[str]) -> None:
    if cp.stdout:
        sys.stdout.write(cp.stdout)
        if not cp.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if cp.stderr:
        sys.stderr.write(cp.stderr)
        if not cp.stderr.endswith("\n"):
            sys.stderr.write("\n")


def _run_manifest_task(
    *,
    cmd: list[str],
    cwd: Path,
    task_name: str,
    project_id: str,
) -> subprocess.CompletedProcess[str]:
    env: dict[str, str] | None = None
    if _is_pdm_command(cmd):
        env = dict(os.environ)
        # Avoid in-project `.venv` on bind mounts / filesystem layers that can produce undeletable or un-renamable
        # entries when `virtualenv` attempts symlinks. Keeping venvs under XDG dirs (pointed at /tmp) is more robust.
        env.setdefault("PDM_VENV_IN_PROJECT", "0")
        env.setdefault("XDG_DATA_HOME", "/tmp/scaffold_xdg_data")
        env.setdefault("XDG_STATE_HOME", "/tmp/scaffold_xdg_state")
        env.setdefault("XDG_CACHE_HOME", "/tmp/scaffold_xdg_cache")
        # Some environments (including certain containerized/CI setups) disallow creating symlinks inside bind mounts.
        # PDM's default `venv.backend=virtualenv` may attempt to symlink the interpreter into the venv, which fails with
        # `PermissionError: [Errno 1] Operation not permitted`.
        #
        # `virtualenv` honors `VIRTUALENV_COPIES=1` and will copy instead of symlinking, making `pdm` tasks more reliable.
        env.setdefault("VIRTUALENV_COPIES", "1")

    if task_name != "install" or not _is_pdm_install_command(cmd):
        if env is None:
            return _run(cmd, cwd=cwd)
        return _run(cmd, cwd=cwd, env=env)

    first = _run(cmd, cwd=cwd, capture=True, env=env)
    _emit_captured_process_output(first)
    if first.returncode == 0:
        return first

    if not _looks_like_transient_pdm_local_path_failure(
        stdout=first.stdout or "",
        stderr=first.stderr or "",
    ):
        return first

    _eprint(
        "WARNING: transient PDM local-path resolution failure detected for "
        f"project '{project_id}'. Retrying install once."
    )
    second = _run(cmd, cwd=cwd, capture=True, env=env)
    _emit_captured_process_output(second)
    return second


def _parse_vars(kv_list: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in kv_list:
        if "=" not in item:
            raise ScaffoldError(f"Invalid --vars value (expected k=v): {item}")
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            raise ScaffoldError(f"Invalid --vars key in: {item}")
        if k in out:
            raise ScaffoldError(f"Duplicate --vars key: {k}")
        out[k] = v
    return out


def _validate_simple_name(name: str) -> None:
    if name.strip() != name:
        raise ScaffoldError("Name must not have leading/trailing whitespace.")
    if name in {".", ".."}:
        raise ScaffoldError("Name must not be '.' or '..'.")
    if "\x00" in name:
        raise ScaffoldError("Name must not contain NUL bytes.")

    seps = {"/", "\\"}
    if os.path.sep:
        seps.add(os.path.sep)
    if os.path.altsep:
        seps.add(os.path.altsep)
    if any(sep in name for sep in seps):
        raise ScaffoldError("Name must not contain path separators.")


def _normalize_repo_rel_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    while normalized.startswith("/"):
        normalized = normalized[1:]
    while normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


_SNAKE_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")


def _to_snake(name: str) -> str:
    snake = _SNAKE_NON_ALNUM_RE.sub("_", name.strip()).strip("_").lower()
    return snake or "project"


def _is_windows_drive_path(value: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:[\\\\/]", value))


def _classify_source(repo_root: Path, source: str) -> tuple[str, Path | None]:
    if source.startswith("gh:") or source.startswith("git@"):
        return "external", None

    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "ssh", "git", "file"}:
        return "external", None

    if _is_windows_drive_path(source):
        path = Path(source)
        if path.exists():
            return "local", path
        return "local_missing", path

    path = (repo_root / source).resolve()
    if path.exists():
        return "local", path
    return "local_missing", path


def _format_with_context(template: str, context: dict[str, str]) -> str:
    try:
        return template.format_map(context)
    except KeyError as exc:
        raise ScaffoldError(f"Unknown placeholder in template string: {exc}") from exc


def _build_context(
    *,
    name: str,
    kind: str,
    dest_rel: Path,
    dest_abs: Path,
    extra_vars: dict[str, str],
) -> dict[str, str]:
    ctx: dict[str, str] = {
        "name": name,
        "kind": kind,
        "name_snake": _to_snake(name),
        "dest_path": str(dest_rel).replace("\\", "/"),
        "dest_dir": str(dest_abs),
    }
    for k, v in extra_vars.items():
        ctx[k] = v
    return ctx


def _load_registry(repo_root: Path) -> dict[str, Any]:
    registry = _load_toml(_registry_path(repo_root))
    if "kinds" not in registry or "generators" not in registry:
        raise ScaffoldError("registry.toml must contain [kinds.*] and [generators.*] tables.")
    return registry


def _get_kind(registry: dict[str, Any], kind: str) -> dict[str, Any]:
    kinds = registry.get("kinds", {})
    if not isinstance(kinds, dict) or kind not in kinds or not isinstance(kinds[kind], dict):
        raise ScaffoldError(f"Unknown kind: {kind}")
    return cast(dict[str, Any], kinds[kind])


def _get_generator(registry: dict[str, Any], generator_id: str) -> dict[str, Any]:
    generators = registry.get("generators", {})
    if (
        not isinstance(generators, dict)
        or generator_id not in generators
        or not isinstance(generators[generator_id], dict)
    ):
        raise ScaffoldError(f"Unknown generator: {generator_id}")
    gen = dict(generators[generator_id])
    gen.setdefault("id", generator_id)
    return gen


def _require_generator_str(generator: dict[str, Any], key: str) -> str:
    v = generator.get(key)
    if not isinstance(v, str) or not v:
        raise ScaffoldError(f"generators.{generator['id']}.{key} must be a non-empty string")
    return v


def _validate_task_cmd(cmd: Any, *, where: str) -> list[str]:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        raise ScaffoldError(f"{where}: expected a non-empty string list command")
    return cmd


def _require_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ScaffoldError(f"{where} must be a boolean")
    return value


def _ci_flag(ci: dict[str, Any], key: str, *, where_prefix: str) -> bool:
    if key not in ci:
        return False
    return _require_bool(ci[key], where=f"{where_prefix}.{key}")


def _validate_ci_tasks(
    *,
    ci: dict[str, Any],
    tasks: dict[str, list[str]],
    kind: str,
    generator_id: str,
    project_id: str,
    allow_missing: bool,
) -> None:
    required: list[str] = []
    where_prefix = f"kinds.{kind}.ci"
    for task_name in ("lint", "test", "build"):
        if _ci_flag(ci, task_name, where_prefix=where_prefix) and task_name not in tasks:
            required.append(task_name)

    if not required:
        return

    msg = (
        f"Kind '{kind}' CI enables {', '.join(required)}, but generator '{generator_id}' did not define "
        + ", ".join(f"tasks.{t}" for t in required)
        + "."
    )
    if not allow_missing:
        raise ScaffoldError(msg + " Fix the generator tasks or disable those CI flags for the kind.")

    _eprint("WARNING:", msg)
    _eprint(f"WARNING: Proceeding due to --allow-missing-ci-tasks; CI may fail for project '{project_id}'.")


def _normalize_tasks(tasks: Any, *, where: str) -> dict[str, list[str]]:
    if tasks is None:
        return {}
    if not isinstance(tasks, dict):
        raise ScaffoldError(f"{where}: expected tasks table")
    out: dict[str, list[str]] = {}
    for task_name, cmd in tasks.items():
        if not isinstance(task_name, str) or not task_name:
            raise ScaffoldError(f"{where}: invalid task name {task_name!r}")
        _validate_task_name(task_name, where=where)
        out[task_name] = _validate_task_cmd(cmd, where=f"{where}.tasks.{task_name}")
    return out


def _validate_task_name(task_name: str, *, where: str) -> None:
    """Validate a task name is safe to serialize as `tasks.<name>` in TOML."""
    if not _TOML_BARE_KEY_RE.match(task_name):
        raise ScaffoldError(f"{where}: task name must match ^[a-zA-Z0-9_-]+$ (rejects dots and spaces): {task_name!r}")


def _ruff_check_with_fix(cmd: list[str]) -> list[str] | None:
    """Return a ruff-check command with `--fix` inserted, or None if not recognized."""
    if "--fix" in cmd:
        return cmd
    for i in range(len(cmd) - 1):
        if Path(cmd[i]).name.lower() in {"ruff", "ruff.exe", "ruff.cmd", "ruff.bat"} and cmd[i + 1] == "check":
            return [*cmd[: i + 2], "--fix", *cmd[i + 2 :]]
    return None


def _ensure_unique_project_id(projects: list[dict[str, Any]], project_id: str) -> None:
    for project in projects:
        if project.get("id") == project_id:
            raise ScaffoldError(f"Project id already exists in manifest: {project_id}")


def _copy_tree_with_substitutions(
    *,
    source_dir: Path,
    dest_dir: Path,
    substitutions: dict[str, str],
) -> None:
    shutil.copytree(source_dir, dest_dir)
    if not substitutions:
        return

    # Rename paths bottom-up so renames don't invalidate traversal.
    all_paths: list[Path] = sorted(dest_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    all_paths.append(dest_dir)
    for path in all_paths:
        new_name = path.name
        for token, replacement in substitutions.items():
            new_name = new_name.replace(token, replacement)
        if new_name != path.name:
            target = path.with_name(new_name)
            if target.exists():
                raise ScaffoldError(f"Substitution rename collision: {target}")
            path.rename(target)

    # Replace contents in text files.
    for file_path in dest_dir.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text = text
        for token, replacement in substitutions.items():
            new_text = new_text.replace(token, replacement)
        if new_text != text:
            file_path.write_text(new_text, encoding="utf-8")


@dataclasses.dataclass(frozen=True)
class GeneratedProjectInfo:
    path: str
    toolchain: str
    package_manager: str
    tasks: dict[str, list[str]]
    warnings: list[str]
    provenance: dict[str, Any]


def _generate_copy(
    *,
    repo_root: Path,
    generator: dict[str, Any],
    dest_dir: Path,
    context: dict[str, str],
) -> GeneratedProjectInfo:
    source = _require_generator_str(generator, "source")

    source_kind, source_path = _classify_source(repo_root, source)
    if source_kind != "local" or source_path is None:
        raise ScaffoldError(f"copy generator source must be a local directory path: {source}")
    if not source_path.is_dir():
        raise ScaffoldError(f"copy generator source is not a directory: {source_path}")

    substitutions_raw = generator.get("substitutions", {})
    substitutions: dict[str, str] = {}
    if substitutions_raw is not None:
        if not isinstance(substitutions_raw, dict):
            raise ScaffoldError("copy generator substitutions must be a table of string->string")
        for token, tmpl in substitutions_raw.items():
            if not isinstance(token, str) or not isinstance(tmpl, str):
                raise ScaffoldError("copy generator substitutions must be a table of string->string")
            substitutions[token] = _format_with_context(tmpl, context)

    _copy_tree_with_substitutions(source_dir=source_path, dest_dir=dest_dir, substitutions=substitutions)

    toolchain = _require_generator_str(generator, "toolchain")
    package_manager = _require_generator_str(generator, "package_manager")
    tasks = _normalize_tasks(generator.get("tasks"), where=f"generators.{generator['id']}")
    return GeneratedProjectInfo(
        path=str(dest_dir.relative_to(repo_root)).replace("\\", "/"),
        toolchain=toolchain,
        package_manager=package_manager,
        tasks=tasks,
        warnings=[],
        provenance={},
    )


def _git_clone_or_fetch(*, repo_dir: Path, source: str) -> None:
    if repo_dir.exists() and not repo_dir.is_dir():
        raise ScaffoldError(f"Cache path exists but is not a directory: {repo_dir}")

    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        cp = _run(["git", "clone", "--no-checkout", source, str(repo_dir)], cwd=repo_dir.parent)
        if cp.returncode != 0:
            raise ScaffoldError(f"git clone failed ({cp.returncode})")
        return

    cp = _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=repo_dir)
    if cp.returncode != 0:
        raise ScaffoldError(f"git fetch failed ({cp.returncode})")


def _git_checkout_clean(*, repo_dir: Path, ref: str) -> str:
    cp = _run(["git", "checkout", "--force", ref], cwd=repo_dir)
    if cp.returncode != 0:
        raise ScaffoldError(f"git checkout failed ({cp.returncode})")

    _run(["git", "clean", "-fdx"], cwd=repo_dir)

    cp2 = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture=True)
    if cp2.returncode != 0:
        raise ScaffoldError(f"git rev-parse failed ({cp2.returncode})")
    return (cp2.stdout or "").strip()


def _git_checkout_origin_head(*, repo_dir: Path) -> str:
    cp = _run(["git", "checkout", "--force", "--detach", "origin/HEAD"], cwd=repo_dir)
    if cp.returncode != 0:
        raise ScaffoldError(f"git checkout origin/HEAD failed ({cp.returncode})")
    cp2 = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture=True)
    if cp2.returncode != 0:
        raise ScaffoldError(f"git rev-parse failed ({cp2.returncode})")
    return (cp2.stdout or "").strip()


def _generate_cookiecutter(
    *,
    repo_root: Path,
    registry: dict[str, Any],
    generator: dict[str, Any],
    dest_dir: Path,
    context: dict[str, str],
    user_vars: dict[str, str],
    trust_external: bool,
    allow_unpinned: bool,
) -> GeneratedProjectInfo:
    _require_on_path("cookiecutter", why="cookiecutter generator selected")

    source = _require_generator_str(generator, "source")
    toolchain = _require_generator_str(generator, "toolchain")
    package_manager = _require_generator_str(generator, "package_manager")

    ref = generator.get("ref")
    directory = generator.get("directory")
    trusted = bool(generator.get("trusted", False))

    source_kind, local_path = _classify_source(repo_root, source)

    provenance: dict[str, Any] = {}
    template_path: Path

    if source_kind == "external":
        if not trusted and not trust_external:
            raise ScaffoldError(
                f"Refusing to run untrusted external template '{generator['id']}'. Re-run with --trust, or vendor it."
            )
        if not allow_unpinned and not (isinstance(ref, str) and ref):
            raise ScaffoldError(
                f"External generator '{generator['id']}' is missing 'ref'. "
                f"Set generators.{generator['id']}.ref or use --allow-unpinned."
            )

        _require_on_path("git", why="external cookiecutter source selected")

        cache_root = str(registry.get("scaffold", {}).get("templates_cache_dir", ".scaffold/cache"))
        cache_dir = repo_root / cache_root / "cookiecutter" / str(generator["id"])
        _git_clone_or_fetch(repo_dir=cache_dir, source=source)

        pinned = False
        if isinstance(ref, str) and ref:
            resolved_commit = _git_checkout_clean(repo_dir=cache_dir, ref=ref)
            pinned = True
            provenance["generator_ref"] = ref
        else:
            _eprint(f"WARNING: generating from external template without a pinned ref: {source}")
            resolved_commit = _git_checkout_origin_head(repo_dir=cache_dir)
            pinned = False

        provenance.update(
            {
                "generator_source": source,
                "generator_resolved_commit": resolved_commit,
                "generator_pinned": pinned,
            }
        )
        template_path = cache_dir
    elif source_kind == "local" and local_path is not None:
        template_path = local_path
    else:
        raise ScaffoldError(f"cookiecutter generator source does not exist: {source}")

    if directory is not None:
        if not isinstance(directory, str) or not directory:
            raise ScaffoldError("cookiecutter generator 'directory' must be a non-empty string")
        template_path = template_path / directory

    if not template_path.exists():
        raise ScaffoldError(f"cookiecutter template path does not exist: {template_path}")

    context_defaults = generator.get("context_defaults", {})
    extra_context: dict[str, str] = {}
    if context_defaults is not None:
        if not isinstance(context_defaults, dict):
            raise ScaffoldError("cookiecutter generator context_defaults must be a table")
        for k, v in context_defaults.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ScaffoldError("cookiecutter generator context_defaults must be a table of string->string")
            extra_context[k] = _format_with_context(v, context)

    for k, v in user_vars.items():
        extra_context[k] = v

    name_var = generator.get("name_var")
    if isinstance(name_var, str) and name_var:
        extra_context.setdefault(name_var, context["name"])

    with tempfile.TemporaryDirectory(prefix="scaffold_cookiecutter_") as tmp:
        tmp_path = Path(tmp)
        extra_kv = [f"{k}={extra_context[k]}" for k in sorted(extra_context)]
        cp = _run(
            [
                "cookiecutter",
                str(template_path),
                "--no-input",
                "--output-dir",
                str(tmp_path),
                *extra_kv,
            ],
            cwd=repo_root,
        )
        if cp.returncode != 0:
            raise ScaffoldError(f"cookiecutter failed ({cp.returncode})")

        created_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
        if len(created_dirs) != 1:
            raise ScaffoldError(
                f"cookiecutter output unexpected: expected 1 directory in temp output, found {len(created_dirs)}"
            )
        generated_dir = created_dirs[0]
        shutil.move(str(generated_dir), str(dest_dir))

    tasks = _normalize_tasks(generator.get("tasks"), where=f"generators.{generator['id']}")
    warnings: list[str] = []
    if source_kind == "external":
        warnings.append("External cookiecutter templates may execute code via hooks.")

    return GeneratedProjectInfo(
        path=str(dest_dir.relative_to(repo_root)).replace("\\", "/"),
        toolchain=toolchain,
        package_manager=package_manager,
        tasks=tasks,
        warnings=warnings,
        provenance=provenance,
    )


def _generate_command(
    *,
    repo_root: Path,
    generator: dict[str, Any],
    dest_dir: Path,
    context: dict[str, str],
) -> GeneratedProjectInfo:
    command = generator.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ScaffoldError("command generator requires 'command' as a non-empty string list")

    formatted = [_format_with_context(arg, context) for arg in command]

    env = dict(os.environ)
    for k, v in context.items():
        env_key = f"SCAFFOLD_VAR_{k.upper()}"
        env[env_key] = v

    cp = _run(formatted, cwd=repo_root, env=env)
    if cp.returncode != 0:
        raise ScaffoldError(f"command generator failed ({cp.returncode})")
    if not dest_dir.exists():
        raise ScaffoldError(f"command generator completed but destination does not exist: {dest_dir}")

    toolchain = _require_generator_str(generator, "toolchain")
    package_manager = _require_generator_str(generator, "package_manager")
    tasks = _normalize_tasks(generator.get("tasks"), where=f"generators.{generator['id']}")
    return GeneratedProjectInfo(
        path=str(dest_dir.relative_to(repo_root)).replace("\\", "/"),
        toolchain=toolchain,
        package_manager=package_manager,
        tasks=tasks,
        warnings=[],
        provenance={},
    )


def cmd_init(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry_file = _registry_path(repo_root)
    manifest_file = _manifest_path(repo_root)

    created: list[str] = []

    if not registry_file.exists():
        raise ScaffoldError(f"Missing registry file: {registry_file} (this repo should have been bootstrapped already)")

    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    if not manifest_file.exists():
        _write_manifest(manifest_file, {"schema_version": _MANIFEST_SCHEMA_VERSION, "projects": []})
        created.append(str(manifest_file))

    registry = _load_registry(repo_root)
    cache_dir = str(registry.get("scaffold", {}).get("templates_cache_dir", ".scaffold/cache"))
    vendor_dir = str(registry.get("scaffold", {}).get("vendor_dir", "tools/templates/vendor"))
    for rel in (cache_dir, vendor_dir):
        dir_path = repo_root / rel
        if not dir_path.exists():
            dir_path.mkdir(parents=True, exist_ok=True)
            created.append(str(dir_path))

    if created:
        print("Created:")
        for created_path in created:
            print(f"- {created_path}")
    else:
        print("Nothing to do (already initialized).")

    return 0


def cmd_generators(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry = _load_registry(repo_root)
    generators = registry.get("generators", {})
    if not isinstance(generators, dict):
        raise ScaffoldError("registry.toml: generators must be a table")

    for gen_id, cfg in sorted(generators.items()):
        if not isinstance(cfg, dict):
            continue
        gen_type = cfg.get("type")
        src = cfg.get("source")
        trust = cfg.get("trusted", False)
        ref = cfg.get("ref")

        origin = "n/a"
        if isinstance(src, str):
            sk, _ = _classify_source(repo_root, src)
            origin = sk

        line = f"{gen_id}\ttype={gen_type}\torigin={origin}"
        if isinstance(src, str):
            line += f"\tsource={src}"
        if isinstance(ref, str) and ref:
            line += f"\tref={ref}"
        if gen_type == "cookiecutter" and origin == "external":
            line += f"\ttrusted={bool(trust)}"
        print(line)

    return 0


def cmd_kinds(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry = _load_registry(repo_root)
    kinds = registry.get("kinds", {})
    if not isinstance(kinds, dict):
        raise ScaffoldError("registry.toml: kinds must be a table")
    for kind, cfg in sorted(kinds.items()):
        if not isinstance(cfg, dict):
            continue
        output_dir = cfg.get("output_dir")
        default_gen = cfg.get("default_generator")
        print(f"{kind}\toutput_dir={output_dir}\tdefault_generator={default_gen}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry = _load_registry(repo_root)

    kind = args.kind
    name = args.name
    _validate_simple_name(name)

    kind_cfg = _get_kind(registry, kind)
    output_dir = kind_cfg.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ScaffoldError(f"kinds.{kind}.output_dir must be a non-empty string")

    generator_id = args.generator
    if generator_id is None:
        generator_id = kind_cfg.get("default_generator")
    if not isinstance(generator_id, str) or not generator_id:
        raise ScaffoldError(
            f"No generator selected for kind '{kind}'. Set kinds.{kind}.default_generator or pass --generator."
        )

    generator = _get_generator(registry, generator_id)
    gen_type = generator.get("type")
    if gen_type not in {"copy", "cookiecutter", "command"}:
        raise ScaffoldError(f"Unknown generator type: {gen_type}")

    dest_rel = Path(output_dir) / name
    dest_dir = repo_root / dest_rel
    if dest_dir.exists():
        raise ScaffoldError(f"Destination already exists: {dest_rel}")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)

    user_vars = _parse_vars(args.vars or [])
    context = _build_context(name=name, kind=kind, dest_rel=dest_rel, dest_abs=dest_dir, extra_vars=user_vars)

    if gen_type == "copy":
        info = _generate_copy(repo_root=repo_root, generator=generator, dest_dir=dest_dir, context=context)
    elif gen_type == "cookiecutter":
        info = _generate_cookiecutter(
            repo_root=repo_root,
            registry=registry,
            generator=generator,
            dest_dir=dest_dir,
            context=context,
            user_vars=user_vars,
            trust_external=bool(args.trust),
            allow_unpinned=bool(args.allow_unpinned),
        )
    else:
        info = _generate_command(repo_root=repo_root, generator=generator, dest_dir=dest_dir, context=context)

    manifest = _load_manifest(repo_root)
    projects_raw = manifest.get("projects", [])
    if projects_raw is None:
        projects_raw = []
    if not isinstance(projects_raw, list):
        raise ScaffoldError("monorepo.toml: expected [[projects]] array")
    for project in projects_raw:
        if not isinstance(project, dict):
            raise ScaffoldError("monorepo.toml: each [[projects]] entry must be a table")
    projects: list[dict[str, Any]] = cast(list[dict[str, Any]], projects_raw)
    project_id = name
    _ensure_unique_project_id(projects, project_id)

    ci = kind_cfg.get("ci", {"lint": False, "test": False, "build": False})
    if not isinstance(ci, dict):
        raise ScaffoldError(f"kinds.{kind}.ci must be a table")

    _validate_ci_tasks(
        ci=ci,
        tasks=info.tasks,
        kind=kind,
        generator_id=generator_id,
        project_id=project_id,
        allow_missing=bool(args.allow_missing_ci_tasks),
    )

    project_entry: dict[str, Any] = {
        "id": project_id,
        "kind": kind,
        "path": str(dest_rel).replace("\\", "/"),
        "generator": generator_id,
        "toolchain": info.toolchain,
        "package_manager": info.package_manager,
        "ci": ci,
        "tasks": info.tasks,
    }
    project_entry.update(info.provenance)

    projects.append(project_entry)
    manifest["projects"] = projects
    _write_manifest(_manifest_path(repo_root), manifest)

    for w in info.warnings:
        _eprint(f"WARNING: {w}")

    if not args.no_install:
        install_cmd = info.tasks.get("install")
        if install_cmd is None:
            print("No install task configured; skipping.")
        else:
            _validate_task_cmd(install_cmd, where=f"projects.{project_id}.tasks.install")
            cp = _run(install_cmd, cwd=dest_dir)
            if cp.returncode != 0:
                raise ScaffoldError(
                    f"Install task failed ({cp.returncode}). Project was created at {dest_rel} and recorded in "
                    "monorepo.toml."
                )

    print(f"Created project {project_id} at {dest_rel}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    projects = _load_projects(repo_root)

    _validate_task_name(args.task, where="scaffold run")
    fix = bool(getattr(args, "fix", False))

    selectors = [bool(args.all), bool(args.kind), bool(args.project)]
    if sum(1 for x in selectors if x) != 1:
        raise ScaffoldError("Select exactly one of --all, --kind, or --project.")

    selected: list[dict[str, Any]]
    if args.all:
        selected = projects
    elif args.kind:
        selected = [p for p in projects if p.get("kind") == args.kind]
    else:
        ids = set(args.project or [])
        selected = [p for p in projects if p.get("id") in ids]
        missing = ids - {p.get("id") for p in selected}
        if missing:
            raise ScaffoldError(f"Unknown project id(s): {', '.join(sorted(str(x) for x in missing))}")

    failures: list[str] = []
    for project in selected:
        project_id = project.get("id")
        path = project.get("path")
        tasks = project.get("tasks", {})
        if not isinstance(project_id, str) or not isinstance(path, str) or not isinstance(tasks, dict):
            raise ScaffoldError("Invalid project entry in monorepo.toml")

        task_name = str(args.task)
        fix_task_name = f"{task_name}_fix"

        cmd = tasks.get(fix_task_name) if fix else tasks.get(task_name)
        if cmd is None:
            if fix:
                base = tasks.get(task_name)
                if base is None:
                    msg = f"{project_id}: missing tasks.{task_name}"
                    if args.skip_missing:
                        _eprint(f"WARNING: {msg} (skipping)")
                        continue
                    raise ScaffoldError(msg)

                base_cmd_list = _validate_task_cmd(base, where=f"projects.{project_id}.tasks.{task_name}")
                fixed = _ruff_check_with_fix(base_cmd_list) if task_name == "lint" else None
                if fixed is None:
                    msg = f"{project_id}: missing tasks.{fix_task_name}"
                    if args.skip_missing:
                        _eprint(f"WARNING: {msg} (skipping)")
                        continue
                    raise ScaffoldError(
                        msg
                        + f" (no known --fix support for tasks.{task_name}; define tasks.{fix_task_name} in the manifest)"
                    )
                cmd_list = fixed
            else:
                msg = f"{project_id}: missing tasks.{task_name}"
                if args.skip_missing:
                    _eprint(f"WARNING: {msg} (skipping)")
                    continue
                raise ScaffoldError(msg)
        else:
            where_task = fix_task_name if fix else task_name
            cmd_list = _validate_task_cmd(cmd, where=f"projects.{project_id}.tasks.{where_task}")

        project_dir = repo_root / path
        if not project_dir.exists():
            raise ScaffoldError(f"{project_id}: project directory does not exist: {path}")

        cp = _run_manifest_task(
            cmd=cmd_list,
            cwd=project_dir,
            task_name=task_name,
            project_id=project_id,
        )
        if cp.returncode != 0:
            failures.append(f"{project_id}:{task_name} ({cp.returncode})")
            if not args.keep_going:
                break

    if failures:
        raise ScaffoldError("Task failures:\n" + "\n".join(f"- {f}" for f in failures))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry = _load_registry(repo_root)
    projects = _load_projects(repo_root)

    errors: list[str] = []
    next_actions: list[str] = []
    skip_tool_checks = bool(getattr(args, "skip_tool_checks", False))

    baseline_timeout_seconds = 3.0
    baseline: dict[str, Any] = {
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "min_version": "3.11",
            "ok": sys.version_info >= (3, 11),
        }
    }
    if not bool(baseline["python"]["ok"]):
        errors.append(
            f"python {baseline['python']['version']} is too old (need {baseline['python']['min_version']}+)"
        )
        next_actions.append("Install Python 3.11+ and re-run doctor.")

    ok, tmp_dir, err = _probe_temp_writable(timeout_seconds=baseline_timeout_seconds)
    baseline["temp"] = {"ok": ok, "dir": tmp_dir, "error": err}
    if not ok:
        errors.append(f"temp directory is not writable: {tmp_dir} ({err})")
        next_actions.append("Fix temp directory permissions/free space and re-run doctor.")

    ok, version_line, err = _probe_tool_version(
        argv=[sys.executable, "-m", "pip", "--version"],
        timeout_seconds=baseline_timeout_seconds,
    )
    baseline["pip"] = {
        "required": False,
        "ok": ok,
        "probe": "python -m pip",
        "version": version_line,
        "error": err,
    }
    if not ok:
        details_parts = [p for p in (version_line, err) if p]
        details = "; ".join(details_parts) if details_parts else "unknown_error"
        # `pip` is helpful for onboarding and remediation (e.g., installing PDM), but it's not required to validate a
        # scaffold manifest. Some managed environments (including PDM/virtualenv-backed ones) may not ship `pip`.
        next_actions.append(_pip_remediation_hint(python_exe=sys.executable))

    git_path = _which("git")
    if git_path is None:
        baseline["git"] = {"ok": False, "probe": "path", "resolved_path": None, "version": None, "error": "missing"}
        if not skip_tool_checks:
            errors.append("git is required but was not found on PATH")
        next_actions.append(_git_remediation_hint())
    else:
        ok, version_line, err = _probe_tool_version(
            argv=[git_path, "--version"],
            timeout_seconds=baseline_timeout_seconds,
        )
        baseline["git"] = {
            "ok": ok,
            "probe": "path",
            "resolved_path": git_path,
            "version": version_line,
            "error": err,
        }
        if not ok:
            details_parts = [p for p in (version_line, err) if p]
            details = "; ".join(details_parts) if details_parts else "unknown_error"
            if not skip_tool_checks:
                errors.append(f"git is required but not usable: {details}")
            next_actions.append(_git_remediation_hint())

    bash_required = os.name != "nt"
    bash_path = _which("bash")
    if bash_path is None:
        baseline["bash"] = {
            "required": bash_required,
            "ok": False,
            "probe": "path",
            "resolved_path": None,
            "version": None,
            "error": "missing",
        }
        if bash_required and not skip_tool_checks:
            errors.append("bash is required (for scripts/smoke.sh) but was not found on PATH")
    else:
        ok, version_line, err = _probe_tool_version(
            argv=[bash_path, "-lc", "echo ok"],
            timeout_seconds=2.0,
        )
        baseline["bash"] = {
            "required": bash_required,
            "ok": ok,
            "probe": "bash -lc",
            "resolved_path": bash_path,
            "version": version_line,
            "error": err,
        }
        if bash_required and not ok and not skip_tool_checks:
            details = err or "unknown_error"
            errors.append(f"bash is required (for scripts/smoke.sh) but not usable: {details}")
            next_actions.append(_bash_remediation_hint())
        elif not ok:
            next_actions.append(_bash_remediation_hint())

    required_tools: dict[str, list[str]] = {}
    for project in projects:
        project_id = project.get("id")
        kind = project.get("kind")
        generator_id = project.get("generator")
        path = project.get("path")
        if not isinstance(project_id, str):
            errors.append("Project missing string 'id'")
            continue

        if not isinstance(path, str) or not path:
            errors.append(f"{project_id}: missing/invalid path")
        else:
            project_dir = repo_root / path
            if not project_dir.exists():
                errors.append(f"{project_id}: project directory does not exist: {path}")

        try:
            _get_kind(registry, str(kind))
        except ScaffoldError as exc:
            errors.append(f"{project_id}: {exc}")

        try:
            _get_generator(registry, str(generator_id))
        except ScaffoldError as exc:
            errors.append(f"{project_id}: {exc}")

        tasks = project.get("tasks", {})
        if not isinstance(tasks, dict):
            errors.append(f"{project_id}: tasks must be a table")
            continue

        validated_task_cmds: dict[str, list[str]] = {}
        for task_name, cmd in tasks.items():
            if not isinstance(task_name, str) or not task_name:
                errors.append(f"{project_id}: tasks contains a non-string or empty task name")
                continue
            try:
                validated_task_cmds[task_name] = _validate_task_cmd(
                    cmd, where=f"projects.{project_id}.tasks.{task_name}"
                )
            except ScaffoldError as exc:
                errors.append(f"{project_id}: {exc}")

        ci = project.get("ci", {})
        if ci is not None:
            if not isinstance(ci, dict):
                errors.append(f"{project_id}: ci must be a table when present")
            else:
                for task_name in ("lint", "test", "build"):
                    if task_name in ci:
                        try:
                            enabled = _require_bool(ci[task_name], where=f"projects.{project_id}.ci.{task_name}")
                        except ScaffoldError as exc:
                            errors.append(f"{project_id}: {exc}")
                            continue
                        if enabled and task_name not in tasks:
                            errors.append(f"{project_id}: ci.{task_name} is true but tasks.{task_name} is missing")

        required_tasks: set[str] = set()
        if "install" in validated_task_cmds:
            required_tasks.add("install")
        if isinstance(ci, dict):
            for task_name in ("lint", "test", "build"):
                if ci.get(task_name) is True:
                    required_tasks.add(task_name)

        for task_name in sorted(required_tasks):
            cmd = validated_task_cmds.get(task_name)
            if cmd is None:
                continue
            tool = str(cmd[0])
            required_tools.setdefault(tool, []).append(f"{project_id}:{task_name}")

    tool_timeout_seconds = 4.0
    tool_report: dict[str, Any] = {
        "kind": "scaffold_doctor_tool_report",
        "generated_at": (
            _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ),
        "python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "baseline": baseline,
        "tools": {},
    }

    for tool, contexts in sorted(required_tools.items(), key=lambda kv: kv[0]):
        resolved = _which(tool)
        entry: dict[str, Any] = {"required_by": contexts, "resolved_path": resolved}

        tool_l = tool.lower()
        if tool_l in {"pdm", "pdm.exe", "pdm.cmd", "pdm.bat"}:
            if os.name == "nt" and _pdm_importable():
                ok, version, err = _probe_tool_version(
                    argv=[sys.executable, str(repo_root / "tools" / "pdm_shim.py"), "--version"],
                    timeout_seconds=tool_timeout_seconds,
                )
                entry.update({"probe": "shim", "ok": ok, "version": version, "error": err})
            elif resolved is None:
                entry.update({"probe": "path", "ok": False, "version": None, "error": "missing"})
            else:
                ok, version, err = _probe_tool_version(
                    argv=[resolved, "--version"],
                    timeout_seconds=tool_timeout_seconds,
                )
                entry.update({"probe": "path", "ok": ok, "version": version, "error": err})
                if os.name == "nt" and not ok and not _pdm_importable():
                    entry["remediation"] = (
                        f"Install pdm into this Python to enable the shim: {sys.executable} -m pip install -U pdm"
                    )
        else:
            if resolved is None:
                entry.update({"probe": "path", "ok": False, "version": None, "error": "missing"})
            else:
                ok, version, err = _probe_tool_version(
                    argv=[resolved, "--version"],
                    timeout_seconds=tool_timeout_seconds,
                )
                entry.update({"probe": "path", "ok": ok, "version": version, "error": err})

        tool_report["tools"][tool] = entry

        if not skip_tool_checks and not bool(entry.get("ok")):
            details = entry.get("error") or "unknown_error"
            hint = entry.get("remediation")
            ctx = ", ".join(contexts[:3]) + ("..." if len(contexts) > 3 else "")
            if hint:
                errors.append(f"tool {tool!r} is required (by {ctx}) but not usable: {details} (hint: {hint})")
            else:
                errors.append(f"tool {tool!r} is required (by {ctx}) but not usable: {details}")
        elif not bool(entry.get("ok")):
            hint = entry.get("remediation")
            if hint:
                next_actions.append(hint)

    report_path = _write_doctor_tool_report(repo_root=repo_root, payload=tool_report)

    _eprint(f"Doctor: {'PASS' if not errors else 'FAIL'}")
    _eprint(f"Repo: {repo_root}")
    _eprint("==> Baseline preflight")
    py = cast(dict[str, Any], baseline.get("python", {}))
    py_ok = bool(py.get("ok"))
    py_ver = py.get("version") or "unknown"
    _eprint(f"    - python: {'OK' if py_ok else 'NOT OK'} ({py_ver})")
    tmp = cast(dict[str, Any], baseline.get("temp", {}))
    tmp_suffix = f" ({tmp.get('error')})" if tmp.get("error") else ""
    _eprint(f"    - temp: {'OK' if bool(tmp.get('ok')) else 'NOT OK'} ({tmp.get('dir') or 'unknown'}){tmp_suffix}")
    pip = cast(dict[str, Any], baseline.get("pip", {}))
    _eprint(f"    - pip: {'OK' if bool(pip.get('ok')) else 'NOT OK'} ({pip.get('version') or pip.get('error') or 'unknown'})")
    git = cast(dict[str, Any], baseline.get("git", {}))
    _eprint(f"    - git: {'OK' if bool(git.get('ok')) else 'NOT OK'} ({git.get('version') or git.get('error') or 'unknown'})")
    bash = cast(dict[str, Any], baseline.get("bash", {}))
    bash_required = bool(bash.get("required"))
    bash_ok = bool(bash.get("ok"))
    if bash_required:
        bash_status = "OK" if bash_ok else "NOT OK"
    else:
        bash_status = "OK" if bash_ok else ("MISSING" if not bash.get("resolved_path") else "NOT OK")
    bash_label = "bash (required)" if bash_required else "bash (optional)"
    _eprint(f"    - {bash_label}: {bash_status} ({bash.get('version') or bash.get('error') or 'unknown'})")

    if required_tools:
        _eprint("==> Tool preflight")
        for tool in sorted(required_tools.keys()):
            entry = cast(dict[str, Any], tool_report["tools"].get(tool, {}))
            ok = bool(entry.get("ok"))
            probe = entry.get("probe") or "unknown"
            version = entry.get("version")
            if ok:
                suffix = f" ({version})" if version else ""
                _eprint(f"    - {tool}: OK via {probe}{suffix}")
            else:
                details = entry.get("error") or "unknown_error"
                _eprint(f"    - {tool}: NOT OK via {probe} ({details})")

    if report_path is not None:
        _eprint(f"==> Tool report: {report_path}")

    if errors:
        _eprint("==> Problems")
        for e in errors:
            _eprint(f"    - {e}")
        actions = _dedup_preserve_order([a for a in next_actions if a.strip()])
        if actions:
            _eprint("==> Next actions")
            for a in actions:
                _eprint(f"    - {a}")
        return 1

    print("OK")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry = _load_registry(repo_root)
    manifest_path = _manifest_path(repo_root)
    manifest = _load_manifest(repo_root)

    projects_raw = manifest.get("projects", [])
    if projects_raw is None:
        projects_raw = []
    if not isinstance(projects_raw, list):
        raise ScaffoldError("monorepo.toml: expected [[projects]] array")

    sync_tasks = bool(getattr(args, "sync_tasks", False))
    sync_ci = bool(getattr(args, "sync_ci", False))
    prune_missing = bool(getattr(args, "prune_missing", False))
    check = bool(getattr(args, "check", False))
    show_diff = bool(getattr(args, "diff", False))

    changes: list[str] = []
    updated_projects: list[dict[str, Any]] = []
    for project in projects_raw:
        if not isinstance(project, dict):
            raise ScaffoldError("monorepo.toml: each [[projects]] entry must be a table")

        project_id = project.get("id")
        kind = project.get("kind")
        generator_id = project.get("generator")
        path_raw = project.get("path")
        if not isinstance(project_id, str) or not project_id:
            raise ScaffoldError("monorepo.toml: projects[].id must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ScaffoldError(f"{project_id}: projects[].kind must be a non-empty string")
        if not isinstance(generator_id, str) or not generator_id:
            raise ScaffoldError(f"{project_id}: projects[].generator must be a non-empty string")
        if not isinstance(path_raw, str) or not path_raw:
            raise ScaffoldError(f"{project_id}: projects[].path must be a non-empty string")

        normalized_path = _normalize_repo_rel_path(path_raw)
        if not normalized_path:
            raise ScaffoldError(f"{project_id}: projects[].path normalizes to empty: {path_raw!r}")
        if normalized_path != path_raw:
            project["path"] = normalized_path
            changes.append(f"{project_id}: normalized path {path_raw!r} -> {normalized_path!r}")

        project_dir = repo_root / normalized_path
        if prune_missing and not project_dir.exists():
            changes.append(f"{project_id}: pruned missing path {normalized_path!r}")
            continue

        kind_cfg = _get_kind(registry, kind)
        generator = _get_generator(registry, generator_id)

        toolchain = _require_generator_str(generator, "toolchain")
        package_manager = _require_generator_str(generator, "package_manager")

        if project.get("toolchain") != toolchain:
            project["toolchain"] = toolchain
            changes.append(f"{project_id}: set toolchain={toolchain!r}")
        if project.get("package_manager") != package_manager:
            project["package_manager"] = package_manager
            changes.append(f"{project_id}: set package_manager={package_manager!r}")

        kind_ci_raw = kind_cfg.get("ci", {"lint": False, "test": False, "build": False})
        if not isinstance(kind_ci_raw, dict):
            raise ScaffoldError(f"kinds.{kind}.ci must be a table")
        kind_ci: dict[str, Any] = {}
        for key in ("lint", "test", "build"):
            if key in kind_ci_raw:
                kind_ci[key] = _require_bool(kind_ci_raw[key], where=f"kinds.{kind}.ci.{key}")

        ci_raw = project.get("ci")
        if sync_ci or ci_raw is None:
            if project.get("ci") != kind_ci:
                project["ci"] = kind_ci
                changes.append(f"{project_id}: synced ci from kinds.{kind}.ci")
        else:
            if not isinstance(ci_raw, dict):
                raise ScaffoldError(f"{project_id}: projects[].ci must be a table when present")
            ci: dict[str, Any] = dict(ci_raw)
            for key in ("lint", "test", "build"):
                if key in ci:
                    ci[key] = _require_bool(ci[key], where=f"projects.{project_id}.ci.{key}")
                elif key in kind_ci:
                    ci[key] = kind_ci[key]
            if ci != ci_raw:
                project["ci"] = ci
                changes.append(f"{project_id}: filled missing ci flags from kinds.{kind}.ci")

        project_tasks = _normalize_tasks(project.get("tasks"), where=f"projects.{project_id}")
        generator_tasks = _normalize_tasks(generator.get("tasks"), where=f"generators.{generator_id}")
        if sync_tasks:
            for task_name, cmd in generator_tasks.items():
                if project_tasks.get(task_name) != cmd:
                    project_tasks[task_name] = cmd
                    changes.append(f"{project_id}: synced tasks.{task_name} from generators.{generator_id}")
        else:
            for task_name, cmd in generator_tasks.items():
                if task_name not in project_tasks:
                    project_tasks[task_name] = cmd
                    changes.append(f"{project_id}: added missing tasks.{task_name} from generators.{generator_id}")
        project["tasks"] = project_tasks

        updated_projects.append(project)

    if prune_missing and len(updated_projects) != len(projects_raw):
        manifest["projects"] = updated_projects
    else:
        manifest["projects"] = projects_raw

    existing_text = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    with tempfile.TemporaryDirectory() as tmp_dir:
        rendered_path = Path(tmp_dir) / "monorepo.toml"
        _write_manifest(rendered_path, manifest)
        new_text = rendered_path.read_text(encoding="utf-8")

    if existing_text == new_text:
        print("Nothing to do.")
        return 0

    if show_diff:
        diff = difflib.unified_diff(
            existing_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(manifest_path),
            tofile=str(manifest_path),
        )
        print("".join(diff).rstrip())

    if check:
        _eprint("Fix would update tools/scaffold/monorepo.toml.")
        if changes:
            for line in changes:
                _eprint(f"- {line}")
        else:
            _eprint("- formatting only")
        return 1

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(new_text, encoding="utf-8")
    if changes:
        print("Updated tools/scaffold/monorepo.toml:")
        for line in changes:
            print(f"- {line}")
    else:
        print("Updated tools/scaffold/monorepo.toml.")
    return 0


def _detect_license_spdx(text: str) -> str:
    t = text.lower()
    if "mit license" in t or "permission is hereby granted" in t:
        return "MIT"
    if "apache license" in t and "version 2.0" in t:
        return "Apache-2.0"
    if "gnu general public license" in t and "version 3" in t:
        return "GPL-3.0"
    if "gnu general public license" in t and "version 2" in t:
        return "GPL-2.0"
    if "redistribution and use in source and binary forms" in t and "disclaimer" in t:
        return "BSD-3-Clause"
    return "unknown"


def _find_license_files(repo_dir: Path) -> list[Path]:
    candidates = [
        "LICENSE",
        "LICENSE.txt",
        "LICENSE.md",
        "COPYING",
        "COPYING.txt",
        "COPYING.md",
    ]
    found: list[Path] = []
    for name in candidates:
        p = repo_dir / name
        if p.exists() and p.is_file():
            found.append(p)
    return found


def _append_generator_to_registry(registry_file: Path, generator_id: str, gen: dict[str, Any]) -> None:
    def line(key: str, value: Any) -> str:
        return f"{key} = {_toml_format_value(value)}"

    lines: list[str] = []
    lines.append("")
    lines.append(f"[generators.{generator_id}]")
    for k in ("type", "source", "trusted", "name_var", "toolchain", "package_manager"):
        if k in gen:
            lines.append(line(k, gen[k]))

    context_defaults = gen.get("context_defaults")
    if isinstance(context_defaults, dict) and context_defaults:
        lines.append(line("context_defaults", context_defaults))

    tasks = gen.get("tasks")
    if isinstance(tasks, dict):
        for task_name, cmd in tasks.items():
            lines.append(line(f"tasks.{task_name}", cmd))

    existing = registry_file.read_text(encoding="utf-8")
    suffix = "" if existing.endswith("\n") else "\n"
    registry_file.write_text(existing + suffix + "\n".join(lines).lstrip("\n") + "\n", encoding="utf-8")


def cmd_vendor_import(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry = _load_registry(repo_root)

    generator = _get_generator(registry, args.generator_id)
    if generator.get("type") != "cookiecutter":
        raise ScaffoldError("vendor import only supports cookiecutter generators")

    source = _require_generator_str(generator, "source")
    source_kind, _ = _classify_source(repo_root, source)
    if source_kind != "external":
        raise ScaffoldError("vendor import expects an external cookiecutter source (URL/gh:/file://)")

    _require_on_path("git", why="vendoring requires cloning upstream")

    upstream_ref = args.ref or generator.get("ref")
    if not isinstance(upstream_ref, str) or not upstream_ref:
        raise ScaffoldError("Upstream ref is required. Set generators.<id>.ref or pass --ref.")

    vendor_id = args.as_id or args.generator_id
    if not re.match(r"^[a-zA-Z0-9_-]+$", vendor_id):
        raise ScaffoldError("Vendor id must match ^[a-zA-Z0-9_-]+$")

    generators_table = registry.get("generators", {})
    if isinstance(generators_table, dict) and vendor_id in generators_table:
        raise ScaffoldError(f"Generator id already exists: {vendor_id}")

    vendor_dir = str(registry.get("scaffold", {}).get("vendor_dir", "tools/templates/vendor"))
    vendor_root = repo_root / vendor_dir
    vendor_path = vendor_root / vendor_id
    if vendor_path.exists():
        raise ScaffoldError(f"Vendor directory already exists: {vendor_path}")

    cache_root = str(registry.get("scaffold", {}).get("templates_cache_dir", ".scaffold/cache"))
    cache_dir = repo_root / cache_root / "cookiecutter" / f"vendor_{args.generator_id}"
    _git_clone_or_fetch(repo_dir=cache_dir, source=source)
    resolved_commit = _git_checkout_clean(repo_dir=cache_dir, ref=upstream_ref)

    template_path = cache_dir
    upstream_directory = generator.get("directory")
    if upstream_directory is not None:
        if not isinstance(upstream_directory, str) or not upstream_directory:
            raise ScaffoldError("cookiecutter generator 'directory' must be a non-empty string")
        template_path = template_path / upstream_directory

    if not template_path.exists():
        raise ScaffoldError(f"Template directory does not exist in upstream checkout: {template_path}")

    vendor_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template_path, vendor_path, ignore=shutil.ignore_patterns(".git"))

    license_files = _find_license_files(cache_dir)
    copied_license_names: list[str] = []
    license_spdx = "unknown"
    for lf in license_files:
        target = vendor_path / lf.name
        if not target.exists():
            shutil.copy2(lf, target)
            copied_license_names.append(lf.name)
        if license_spdx == "unknown":
            try:
                license_spdx = _detect_license_spdx(lf.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                license_spdx = "unknown"

    upstream_meta: dict[str, Any] = {
        "upstream_url": source,
        "upstream_ref": upstream_ref,
        "upstream_resolved_commit": resolved_commit,
        "imported_at": (
            _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ),
        "license_spdx": license_spdx,
        "license_files": copied_license_names,
    }
    if upstream_directory:
        upstream_meta["upstream_directory"] = upstream_directory

    upstream_file = vendor_path / "UPSTREAM.toml"
    upstream_file.write_text(
        "\n".join(f"{k} = {_toml_format_value(v)}" for k, v in upstream_meta.items()) + "\n",
        encoding="utf-8",
    )

    registry_file = _registry_path(repo_root)
    new_gen: dict[str, Any] = {
        "type": "cookiecutter",
        "source": str(Path(vendor_dir) / vendor_id).replace("\\", "/"),
        "trusted": True,
        "toolchain": _require_generator_str(generator, "toolchain"),
        "package_manager": _require_generator_str(generator, "package_manager"),
        "tasks": _normalize_tasks(generator.get("tasks"), where=f"generators.{args.generator_id}"),
    }
    if "name_var" in generator:
        new_gen["name_var"] = generator["name_var"]
    if "context_defaults" in generator:
        new_gen["context_defaults"] = generator["context_defaults"]

    _append_generator_to_registry(registry_file, vendor_id, new_gen)

    print(f"Vendored {args.generator_id} into {vendor_path}")
    print(f"Added generator '{vendor_id}' pointing at tools/templates/vendor/{vendor_id}")
    return 0


def cmd_vendor_update(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    registry = _load_registry(repo_root)

    vendor_dir = str(registry.get("scaffold", {}).get("vendor_dir", "tools/templates/vendor"))
    vendor_root = repo_root / vendor_dir
    vendor_path = vendor_root / args.vendor_id
    if not vendor_path.exists():
        raise ScaffoldError(f"Vendor directory does not exist: {vendor_path}")

    upstream_file = vendor_path / "UPSTREAM.toml"
    if not upstream_file.exists():
        raise ScaffoldError(f"Missing UPSTREAM.toml in vendored template: {upstream_file}")

    upstream = _load_toml(upstream_file)
    upstream_url = upstream.get("upstream_url")
    upstream_ref = args.ref or upstream.get("upstream_ref")
    upstream_directory = upstream.get("upstream_directory")
    if not isinstance(upstream_url, str) or not upstream_url:
        raise ScaffoldError("UPSTREAM.toml missing upstream_url")
    if not isinstance(upstream_ref, str) or not upstream_ref:
        raise ScaffoldError("Upstream ref is required (UPSTREAM.toml upstream_ref or --ref).")
    if upstream_directory is not None and (not isinstance(upstream_directory, str) or not upstream_directory):
        raise ScaffoldError("UPSTREAM.toml upstream_directory must be a non-empty string when present")

    _require_on_path("git", why="vendoring requires cloning upstream")

    cache_root = str(registry.get("scaffold", {}).get("templates_cache_dir", ".scaffold/cache"))
    cache_dir = repo_root / cache_root / "cookiecutter" / f"vendor_update_{args.vendor_id}"
    _git_clone_or_fetch(repo_dir=cache_dir, source=upstream_url)
    _git_checkout_clean(repo_dir=cache_dir, ref=upstream_ref)

    upstream_tmp = vendor_root / f"{args.vendor_id}.__upstream_tmp__"
    current_tmp = vendor_root / f"{args.vendor_id}.__current_tmp__"
    if upstream_tmp.exists() or current_tmp.exists():
        raise ScaffoldError(
            f"Temp directories already exist: {current_tmp.name} / {upstream_tmp.name}. Delete them and retry."
        )

    def ignore_current(dirpath: str, names: list[str]) -> set[str]:
        ignored = set()
        for n in names:
            if n == "UPSTREAM.toml":
                ignored.add(n)
            if n == ".git":
                ignored.add(n)
            if n.endswith(".__upstream_tmp__") or n.endswith(".__current_tmp__"):
                ignored.add(n)
        return ignored

    shutil.copytree(vendor_path, current_tmp, ignore=ignore_current)

    upstream_template_dir = cache_dir / upstream_directory if upstream_directory else cache_dir
    if not upstream_template_dir.exists():
        raise ScaffoldError(f"Upstream template directory missing in checkout: {upstream_template_dir}")
    shutil.copytree(upstream_template_dir, upstream_tmp, ignore=shutil.ignore_patterns(".git"))

    cp = _run(
        ["git", "diff", "--no-index", str(current_tmp), str(upstream_tmp)],
        cwd=repo_root,
        capture=True,
    )
    diff_text = (cp.stdout or "") + (cp.stderr or "")
    print(diff_text.rstrip())
    print("")
    print("Update workflow (manual merge):")
    print("- Review diff above.")
    print(f"- Upstream snapshot: {upstream_tmp}")
    print(f"- Current vendored snapshot (without UPSTREAM.toml): {current_tmp}")
    print(f"- Manually merge changes into: {vendor_path}")
    print(f"- When done, delete temp dirs: {current_tmp.name}, {upstream_tmp.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scaffold")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="Initialize scaffold directories/files (idempotent).")
    p_init.set_defaults(func=cmd_init)

    p_kinds = sub.add_parser("kinds", help="List configured kinds.")
    p_kinds.set_defaults(func=cmd_kinds)

    p_gens = sub.add_parser("generators", help="List configured generators.")
    p_gens.set_defaults(func=cmd_generators)

    p_add = sub.add_parser("add", help="Create a new project and record it in the manifest.")
    p_add.add_argument("kind")
    p_add.add_argument("name")
    p_add.add_argument("--generator", help="Override the kind's default generator.")
    p_add.add_argument("--no-install", action="store_true", help="Skip running tasks.install after generation.")
    p_add.add_argument(
        "--vars",
        action="append",
        default=[],
        help="Template variables as k=v (repeatable). Passed to cookiecutter; also used for substitutions/commands.",
    )
    p_add.add_argument(
        "--trust",
        action="store_true",
        help="Allow running an external cookiecutter generator marked trusted=false for this run.",
    )
    p_add.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="Allow running an external generator without a pinned ref (records this in the manifest).",
    )
    p_add.add_argument(
        "--allow-missing-ci-tasks",
        action="store_true",
        help="Allow creating a project even if kinds.<kind>.ci enables tasks the generator does not define.",
    )
    p_add.set_defaults(func=cmd_add)

    p_run = sub.add_parser("run", help="Run a task across projects using the manifest.")
    p_run.add_argument("task")
    sel = p_run.add_mutually_exclusive_group(required=True)
    sel.add_argument("--all", action="store_true")
    sel.add_argument("--kind")
    sel.add_argument("--project", action="append", default=[])
    p_run.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Run the task in autofix mode when supported. For lint, this uses tasks.lint_fix if present, "
            "otherwise attempts a best-effort transformation (e.g. ruff check --fix)."
        ),
    )
    p_run.add_argument("--skip-missing", action="store_true", help="Skip projects that do not define this task.")
    p_run.add_argument("--keep-going", action="store_true", help="Continue running even if a task fails.")
    p_run.set_defaults(func=cmd_run)

    p_doctor = sub.add_parser(
        "doctor", help="Validate config + manifest and check required tools for existing projects."
    )
    p_doctor.add_argument(
        "--skip-tool-checks",
        action="store_true",
        help=(
            "Skip checking that task command binaries are on PATH. "
            "This keeps manifest/config validation while allowing pip-first flows without pdm."
        ),
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_fix = sub.add_parser("fix", help="Normalize/sync tools/scaffold/monorepo.toml from registry.toml.")
    p_fix.add_argument(
        "--sync-tasks",
        action="store_true",
        help="Overwrite generator-defined tasks in each project from registry.toml (preserves extra project tasks).",
    )
    p_fix.add_argument(
        "--sync-ci",
        action="store_true",
        help="Overwrite each project's ci flags from kinds.<kind>.ci in registry.toml.",
    )
    p_fix.add_argument(
        "--prune-missing",
        action="store_true",
        help="Remove manifest entries whose project directories do not exist.",
    )
    p_fix.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if changes would be made; do not write.",
    )
    p_fix.add_argument(
        "--diff",
        action="store_true",
        help="Print a unified diff of tools/scaffold/monorepo.toml changes.",
    )
    p_fix.set_defaults(func=cmd_fix)

    p_vendor = sub.add_parser("vendor", help="Vendoring helpers for external templates.")
    vendor_sub = p_vendor.add_subparsers(dest="vendor_cmd", required=True)

    p_vi = vendor_sub.add_parser("import", help="Import an external cookiecutter template into tools/templates/vendor.")
    p_vi.add_argument("generator_id")
    p_vi.add_argument("--as", dest="as_id", help="New generator id for the vendored template.")
    p_vi.add_argument("--ref", help="Override upstream ref for this import.")
    p_vi.set_defaults(func=cmd_vendor_import)

    p_vu = vendor_sub.add_parser("update", help="Fetch upstream and stage a diff for manual vendored updates.")
    p_vu.add_argument("vendor_id")
    p_vu.add_argument("--ref", help="Override upstream ref for this update.")
    p_vu.set_defaults(func=cmd_vendor_update)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ScaffoldError as exc:
        _eprint(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
