#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_PYTHON_HEALTH_PROBE = (
    "import encodings, json, os, sys; "
    "print(json.dumps({"
    "'executable': sys.executable, "
    "'version': sys.version.split()[0], "
    "'prefix': sys.prefix, "
    "'base_prefix': getattr(sys, 'base_prefix', None), "
    "'real_prefix': getattr(sys, 'real_prefix', None), "
    "'exec_prefix': sys.exec_prefix, "
    "'base_exec_prefix': getattr(sys, 'base_exec_prefix', None), "
    "'virtual_env': os.environ.get('VIRTUAL_ENV')"
    "}))"
)


@dataclass
class ProbeRecord:
    ok: bool
    command: list[str]
    reason_code: str | None = None
    reason: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PythonRecord:
    ok: bool
    path: str
    reason_code: str | None = None
    reason: str | None = None
    version: str | None = None
    executable: str | None = None
    prefix: str | None = None
    base_prefix: str | None = None
    real_prefix: str | None = None
    exec_prefix: str | None = None
    base_exec_prefix: str | None = None
    virtual_env: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolRecord:
    required: bool
    usable: bool
    command: list[str] = field(default_factory=list)
    source: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VenvRecord:
    required: bool
    usable: bool
    requested_dir: str | None = None
    dir: str | None = None
    python: str | None = None
    source: str | None = None
    fallback_used: bool = False
    reason_code: str | None = None
    reason: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_windows(force_windows: bool) -> bool:
    return force_windows or os.name == "nt"


def _normalize_windows_path(value: str) -> str:
    return value.replace("/", "\\").lower()


def _is_windowsapps_alias(path_text: str | None, *, force_windows: bool) -> bool:
    if not path_text or not _is_windows(force_windows):
        return False
    return "\\windowsapps\\" in _normalize_windows_path(path_text)


def _summarize_text(text: str, *, max_lines: int = 8, max_chars: int = 700) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    lines = normalized.split("\n")
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], "...[truncated]..."]
    joined = "\n".join(lines).strip()
    if len(joined) > max_chars:
        return joined[:max_chars].rstrip() + "...[truncated]..."
    return joined


def _merged_text(stdout: str, stderr: str, extra: str | None = None) -> str:
    return "\n".join(part for part in (stderr, stdout, extra or "") if part).strip()


def _classify_failure(merged: str, *, missing_module: str | None = None) -> tuple[str, str]:
    lowered = merged.lower()
    if "encodings" in lowered and (
        "modulenotfounderror" in lowered or "no module named" in lowered
    ):
        return "missing_stdlib", merged
    if (
        "access is denied" in lowered
        or "permission denied" in lowered
        or "cannot be accessed by the system" in lowered
    ):
        return "access_denied", merged
    if "the system cannot find the file specified" in lowered:
        return "not_found", merged
    if missing_module and (
        f"no module named {missing_module.lower()}" in lowered
        or f"modulenotfounderror: no module named '{missing_module.lower()}'" in lowered
        or f'no module named "{missing_module.lower()}"' in lowered
    ):
        return f"{missing_module.lower()}_missing", merged
    return "command_failed", merged


def _run_command(
    argv: list[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    missing_module: str | None = None,
) -> ProbeRecord:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return ProbeRecord(
            ok=False,
            command=argv,
            reason_code="timeout",
            reason=f"Command timed out after {timeout_seconds:.1f}s.",
            stdout=stdout,
            stderr=stderr,
            exit_code=124,
            timed_out=True,
        )
    except OSError as exc:
        return ProbeRecord(
            ok=False,
            command=argv,
            reason_code="launch_failed",
            reason=str(exc),
            exit_code=1,
        )

    if int(proc.returncode or 0) == 0:
        return ProbeRecord(
            ok=True,
            command=argv,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            exit_code=int(proc.returncode or 0),
        )

    merged = _merged_text(proc.stdout or "", proc.stderr or "")
    reason_code, reason = _classify_failure(merged, missing_module=missing_module)
    return ProbeRecord(
        ok=False,
        command=argv,
        reason_code=reason_code,
        reason=reason,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        exit_code=int(proc.returncode or 0),
    )


def _probe_python(python_exe: str, *, force_windows: bool, timeout_seconds: float) -> PythonRecord:
    raw = str(python_exe or "").strip()
    if not raw:
        return PythonRecord(ok=False, path="", reason_code="not_found", reason="Empty interpreter path.")

    if _is_windowsapps_alias(raw, force_windows=force_windows):
        return PythonRecord(
            ok=False,
            path=raw,
            reason_code="windowsapps_alias",
            reason=(
                "Resolved to a Windows Store / WindowsApps launcher alias. "
                "Select a full CPython interpreter and retry."
            ),
        )

    path_like = (
        os.path.sep in raw
        or (os.path.altsep is not None and os.path.altsep in raw)
        or (":" in raw and _is_windows(force_windows))
    )
    resolved = raw
    if path_like:
        if not Path(raw).exists():
            return PythonRecord(
                ok=False,
                path=raw,
                reason_code="not_found",
                reason=f"Interpreter not found at: {raw}",
            )
    else:
        which = shutil.which(raw)
        if which is None:
            return PythonRecord(
                ok=False,
                path=raw,
                reason_code="not_found",
                reason=f"`{raw}` was not found on PATH.",
            )
        resolved = which
        if _is_windowsapps_alias(resolved, force_windows=force_windows):
            return PythonRecord(
                ok=False,
                path=resolved,
                reason_code="windowsapps_alias",
                reason=(
                    "Resolved to a Windows Store / WindowsApps launcher alias. "
                    "Select a full CPython interpreter and retry."
                ),
            )

    probe = _run_command(
        [resolved, "-c", _PYTHON_HEALTH_PROBE],
        timeout_seconds=timeout_seconds,
        missing_module=None,
    )
    if not probe.ok:
        return PythonRecord(
            ok=False,
            path=resolved,
            reason_code=probe.reason_code,
            reason=probe.reason,
        )

    payload: dict[str, Any] | None = None
    for line in reversed((probe.stdout or "").splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break
    if payload is None:
        return PythonRecord(
            ok=False,
            path=resolved,
            reason_code="runtime_probe_failed",
            reason="Interpreter probe did not emit parseable JSON payload.",
        )

    return PythonRecord(
        ok=True,
        path=resolved,
        version=payload.get("version") if isinstance(payload.get("version"), str) else None,
        executable=payload.get("executable") if isinstance(payload.get("executable"), str) else None,
        prefix=payload.get("prefix") if isinstance(payload.get("prefix"), str) else None,
        base_prefix=payload.get("base_prefix")
        if isinstance(payload.get("base_prefix"), str)
        else None,
        real_prefix=payload.get("real_prefix") if isinstance(payload.get("real_prefix"), str) else None,
        exec_prefix=payload.get("exec_prefix") if isinstance(payload.get("exec_prefix"), str) else None,
        base_exec_prefix=payload.get("base_exec_prefix")
        if isinstance(payload.get("base_exec_prefix"), str)
        else None,
        virtual_env=payload.get("virtual_env") if isinstance(payload.get("virtual_env"), str) else None,
    )


def _probe_pip(
    *,
    python_path: str,
    timeout_seconds: float,
    bootstrap: bool,
    cwd: Path,
) -> ToolRecord:
    command = [python_path, "-m", "pip", "--version"]
    probe = _run_command(command, timeout_seconds=timeout_seconds, cwd=cwd, missing_module="pip")
    attempts = [probe.to_dict()]
    if probe.ok:
        return ToolRecord(required=False, usable=True, command=command, source="selected_python", attempts=attempts)

    if bootstrap:
        ensure = _run_command(
            [python_path, "-m", "ensurepip", "--upgrade"],
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )
        attempts.append(ensure.to_dict())
        if ensure.ok:
            reprobe = _run_command(
                command,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
                missing_module="pip",
            )
            attempts.append(reprobe.to_dict())
            if reprobe.ok:
                return ToolRecord(
                    required=False,
                    usable=True,
                    command=command,
                    source="selected_python",
                    attempts=attempts,
                )
            probe = reprobe
        else:
            probe = ensure

    return ToolRecord(
        required=False,
        usable=False,
        command=command,
        source="selected_python",
        reason_code=probe.reason_code,
        reason=probe.reason,
        attempts=attempts,
    )


def _probe_pdm(
    *,
    python_path: str,
    repo_root: Path,
    timeout_seconds: float,
    force_windows: bool,
    path_env: str | None,
) -> ToolRecord:
    attempts: list[dict[str, Any]] = []
    if _is_windows(force_windows):
        primary_command = [python_path, str(repo_root / "tools" / "pdm_shim.py"), "--version"]
    else:
        primary_command = [python_path, "-m", "pdm", "--version"]
    primary = _run_command(
        primary_command,
        timeout_seconds=timeout_seconds,
        cwd=repo_root,
        missing_module="pdm",
    )
    attempts.append(primary.to_dict())
    if primary.ok:
        return ToolRecord(
            required=False,
            usable=True,
            command=primary_command,
            source="selected_python",
            attempts=attempts,
        )

    external = shutil.which("pdm", path=path_env)
    if external:
        secondary_command = [external, "--version"]
        secondary = _run_command(
            secondary_command,
            timeout_seconds=timeout_seconds,
            cwd=repo_root,
            missing_module=None,
        )
        attempts.append(secondary.to_dict())
        if secondary.ok:
            return ToolRecord(
                required=False,
                usable=True,
                command=secondary_command,
                source="path",
                attempts=attempts,
            )
        failure = secondary
    else:
        failure = primary

    return ToolRecord(
        required=False,
        usable=False,
        command=[],
        source=None,
        reason_code=failure.reason_code,
        reason=failure.reason,
        attempts=attempts,
    )


def _venv_python_path(venv_dir: Path, *, force_windows: bool) -> Path:
    if _is_windows(force_windows):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)


def _ensure_venv(
    *,
    requested_dir: Path,
    python_path: str,
    timeout_seconds: float,
    force_windows: bool,
    allow_temp_fallback: bool,
    cwd: Path,
) -> VenvRecord:
    attempts: list[dict[str, Any]] = []
    preferred_python = _venv_python_path(requested_dir, force_windows=force_windows)

    if preferred_python.exists():
        existing = _probe_python(
            str(preferred_python),
            force_windows=force_windows,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(
            {
                "kind": "existing_probe",
                "dir": str(requested_dir),
                "python": str(preferred_python),
                "result": existing.to_dict(),
            }
        )
        if existing.ok:
            return VenvRecord(
                required=True,
                usable=True,
                requested_dir=str(requested_dir),
                dir=str(requested_dir),
                python=str(preferred_python),
                source="existing",
                attempts=attempts,
            )
        _remove_tree(requested_dir)

    targets: list[tuple[Path, str, bool]] = [(requested_dir, "requested", False)]
    if allow_temp_fallback:
        temp_dir = Path(tempfile.mkdtemp(prefix="usertest_venv_"))
        _remove_tree(temp_dir)
        targets.append((temp_dir, "temp_fallback", True))

    last_reason_code = "venv_create_failed"
    last_reason = ""
    for target_dir, source, fallback_used in targets:
        create_probe = _run_command(
            [python_path, "-m", "venv", str(target_dir)],
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )
        attempts.append(
            {
                "kind": "create",
                "dir": str(target_dir),
                "source": source,
                "fallback_used": fallback_used,
                "result": create_probe.to_dict(),
            }
        )
        if not create_probe.ok:
            last_reason_code = "venv_create_failed"
            last_reason = create_probe.reason or "Failed to create virtual environment."
            _remove_tree(target_dir)
            continue

        target_python = _venv_python_path(target_dir, force_windows=force_windows)
        target_probe = _probe_python(
            str(target_python),
            force_windows=force_windows,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(
            {
                "kind": "created_probe",
                "dir": str(target_dir),
                "python": str(target_python),
                "source": source,
                "fallback_used": fallback_used,
                "result": target_probe.to_dict(),
            }
        )
        if target_probe.ok:
            return VenvRecord(
                required=True,
                usable=True,
                requested_dir=str(requested_dir),
                dir=str(target_dir),
                python=str(target_python),
                source=source,
                fallback_used=fallback_used,
                attempts=attempts,
            )
        last_reason_code = target_probe.reason_code or "venv_unusable"
        last_reason = target_probe.reason or "Created virtual environment is unusable."
        _remove_tree(target_dir)

    return VenvRecord(
        required=True,
        usable=False,
        requested_dir=str(requested_dir),
        dir=None,
        python=None,
        source=None,
        fallback_used=False,
        reason_code=last_reason_code,
        reason=last_reason,
        attempts=attempts,
    )


def _build_failure_message(
    *,
    workflow: str,
    python_info: PythonRecord,
    pip_info: ToolRecord,
    pdm_info: ToolRecord,
    venv_info: VenvRecord,
    require_pip: bool,
    require_pdm: bool,
    require_venv: bool,
) -> str:
    lines = [f"Usertest Python toolchain check failed for {workflow}."]
    lines.append("")
    lines.append("Status:")
    if python_info.ok:
        lines.append(
            f"  - python: OK ({python_info.path}"
            + (f", version {python_info.version}" if python_info.version else "")
            + ")"
        )
    else:
        lines.append(
            f"  - python: NOT OK ({python_info.reason_code or 'unknown'}): "
            f"{_summarize_text(python_info.reason or python_info.path)}"
        )

    if require_pip or pip_info.usable:
        status = "OK" if pip_info.usable else "NOT OK"
        detail = pip_info.command or ["python", "-m", "pip", "--version"]
        lines.append(
            f"  - pip: {status} ({' '.join(detail)})"
            + (
                ""
                if pip_info.usable
                else f": {_summarize_text(pip_info.reason or pip_info.reason_code or 'unusable')}"
            )
        )

    if require_pdm or pdm_info.usable or pdm_info.attempts:
        status = "OK" if pdm_info.usable else "NOT OK"
        if pdm_info.usable:
            lines.append(
                f"  - pdm: {status} ({pdm_info.source or 'unknown'} -> {' '.join(pdm_info.command)})"
            )
        else:
            lines.append(
                f"  - pdm: {status} ({pdm_info.reason_code or 'unusable'}): "
                f"{_summarize_text(pdm_info.reason or 'PDM could not be resolved.')}"
            )
            for attempt in pdm_info.attempts:
                command = attempt.get("command")
                reason = attempt.get("reason")
                reason_code = attempt.get("reason_code")
                if isinstance(command, list):
                    lines.append(f"      tried: {' '.join(str(part) for part in command)}")
                if isinstance(reason, str) and reason.strip():
                    lines.append(
                        f"        -> {reason_code or 'failed'}: {_summarize_text(reason)}"
                    )

    if require_venv:
        if venv_info.usable:
            lines.append(
                f"  - venv: OK ({venv_info.dir}"
                + ("; temp fallback used" if venv_info.fallback_used else "")
                + ")"
            )
        else:
            lines.append(
                f"  - venv: NOT OK ({venv_info.reason_code or 'unusable'}): "
                f"{_summarize_text(venv_info.reason or 'Virtual environment could not be created.')}"
            )

    lines.append("")
    lines.append("Recommended fix path:")
    if not python_info.ok:
        lines.append("  1) Install/select a full CPython interpreter (python.org or your package manager).")
        lines.append("  2) Disable Windows App Execution Alias shims for python.exe/python3.exe if applicable.")
        lines.append("  3) Export USERTEST_PYTHON to a known-good interpreter path before rerunning.")
        return "\n".join(lines)

    next_index = 1
    if require_pip and not pip_info.usable:
        lines.append(f"  {next_index}) Bootstrap pip in this interpreter: {python_info.path} -m ensurepip --upgrade")
        next_index += 1
        lines.append(f"  {next_index}) If pip is still missing, install a full CPython with pip included.")
        next_index += 1
    if require_pdm and not pdm_info.usable:
        lines.append(f"  {next_index}) Install pdm into this interpreter: {python_info.path} -m pip install -U pdm")
        next_index += 1
        lines.append(f"  {next_index}) Rerun the same repo workflow; it will reuse the resolved interpreter.")
        next_index += 1
    if require_venv and not venv_info.usable:
        target = venv_info.requested_dir or ".venv"
        lines.append(
            f"  {next_index}) Check write permissions for {target} or remove a broken existing venv before rerunning."
        )
        next_index += 1
        lines.append(
            f"  {next_index}) If the workspace cannot host symlinks/copies, rerun the workflow and let it use the temp-venv fallback."
        )
    return "\n".join(lines)


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _emit_env(
    *,
    shell: str,
    python_info: PythonRecord,
    pip_info: ToolRecord,
    pdm_info: ToolRecord,
    venv_info: VenvRecord,
) -> str:
    env_map = {
        "USERTEST_TOOLCHAIN_PYTHON_EXE": python_info.path,
        "USERTEST_TOOLCHAIN_PYTHON_VERSION": python_info.version or "",
        "USERTEST_TOOLCHAIN_PIP_USABLE": "1" if pip_info.usable else "0",
        "USERTEST_TOOLCHAIN_PIP_COMMAND": " ".join(pip_info.command),
        "USERTEST_TOOLCHAIN_PDM_USABLE": "1" if pdm_info.usable else "0",
        "USERTEST_TOOLCHAIN_PDM_SOURCE": pdm_info.source or "",
        "USERTEST_TOOLCHAIN_PDM_COMMAND": " ".join(pdm_info.command),
        "USERTEST_TOOLCHAIN_VENV_USABLE": "1" if venv_info.usable else "0",
        "USERTEST_TOOLCHAIN_VENV_DIR": venv_info.dir or "",
        "USERTEST_TOOLCHAIN_VENV_PY": venv_info.python or "",
        "USERTEST_TOOLCHAIN_VENV_SOURCE": venv_info.source or "",
        "USERTEST_TOOLCHAIN_VENV_FALLBACK_USED": "1" if venv_info.fallback_used else "0",
    }
    lines: list[str] = []
    if shell == "shell":
        for key, value in env_map.items():
            lines.append(f"export {key}={_shell_quote(value)}")
        return "\n".join(lines)
    for key, value in env_map.items():
        lines.append(f"$env:{key} = {_powershell_quote(value)}")
    return "\n".join(lines)


def _resolve(args: argparse.Namespace) -> tuple[int, dict[str, Any], str | None]:
    repo_root = Path(args.repo_root).resolve()
    python_requested = args.python_exe or os.environ.get("USERTEST_PYTHON") or sys.executable
    force_windows = bool(args.force_windows)

    python_info = _probe_python(
        python_requested,
        force_windows=force_windows,
        timeout_seconds=args.timeout_seconds,
    )

    pip_info = ToolRecord(required=bool(args.require_pip), usable=False)
    pdm_info = ToolRecord(required=bool(args.require_pdm), usable=False)
    venv_info = VenvRecord(required=bool(args.ensure_venv), usable=not bool(args.ensure_venv))

    path_env = os.environ.get("PATH")
    if python_info.ok:
        pip_info = _probe_pip(
            python_path=python_info.path,
            timeout_seconds=args.timeout_seconds,
            bootstrap=bool(args.bootstrap_pip),
            cwd=repo_root,
        )
        pip_info.required = bool(args.require_pip)
        pdm_info = _probe_pdm(
            python_path=python_info.path,
            repo_root=repo_root,
            timeout_seconds=args.timeout_seconds,
            force_windows=force_windows,
            path_env=path_env,
        )
        pdm_info.required = bool(args.require_pdm)
        if args.ensure_venv:
            venv_info = _ensure_venv(
                requested_dir=Path(args.ensure_venv),
                python_path=python_info.path,
                timeout_seconds=args.timeout_seconds,
                force_windows=force_windows,
                allow_temp_fallback=bool(args.allow_temp_venv_fallback),
                cwd=repo_root,
            )

    ok = bool(python_info.ok)
    if args.require_pip:
        ok = ok and bool(pip_info.usable)
    if args.require_pdm:
        ok = ok and bool(pdm_info.usable)
    if args.ensure_venv:
        ok = ok and bool(venv_info.usable)

    payload = {
        "ok": ok,
        "workflow": args.workflow,
        "python": python_info.to_dict(),
        "pip": pip_info.to_dict(),
        "pdm": pdm_info.to_dict(),
        "venv": venv_info.to_dict(),
    }

    message = None
    if not ok:
        message = _build_failure_message(
            workflow=args.workflow,
            python_info=python_info,
            pip_info=pip_info,
            pdm_info=pdm_info,
            venv_info=venv_info,
            require_pip=bool(args.require_pip),
            require_pdm=bool(args.require_pdm),
            require_venv=bool(args.ensure_venv),
        )
    return (0 if ok else 1), payload, message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve the repo's Python toolchain contract.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve", help="Probe python/pip/pdm/venv and emit a shared contract.")
    resolve.add_argument("--repo-root", required=True)
    resolve.add_argument("--python-exe", default=None)
    resolve.add_argument("--workflow", default="workflow")
    resolve.add_argument("--emit", choices=("json", "shell", "powershell"), default="json")
    resolve.add_argument("--timeout-seconds", type=float, default=5.0)
    resolve.add_argument("--force-windows", action="store_true")
    resolve.add_argument("--require-pip", action="store_true")
    resolve.add_argument("--bootstrap-pip", action="store_true")
    resolve.add_argument("--require-pdm", action="store_true")
    resolve.add_argument("--ensure-venv", default=None)
    resolve.add_argument("--allow-temp-venv-fallback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "resolve":
        parser.error(f"Unsupported command: {args.command}")

    exit_code, payload, message = _resolve(args)
    if args.emit == "json":
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        sys.stdout.write("\n")
    elif exit_code == 0:
        sys.stdout.write(
            _emit_env(
                shell=args.emit,
                python_info=PythonRecord(**payload["python"]),
                pip_info=ToolRecord(**payload["pip"]),
                pdm_info=ToolRecord(**payload["pdm"]),
                venv_info=VenvRecord(**payload["venv"]),
            )
        )
        sys.stdout.write("\n")

    if message:
        sys.stderr.write(message + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
