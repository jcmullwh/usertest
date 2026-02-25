from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PYTHON_HEALTH_PROBE = (
    "import encodings, json, sys; "
    "print(json.dumps({'executable': sys.executable, 'version': sys.version.split()[0]}))"
)


def _is_windows_platform() -> bool:
    return os.name == "nt"


def _normalize_windows_path(path_text: str) -> str:
    return path_text.replace("/", "\\").lower()


def _is_windowsapps_alias(path_text: str | None) -> bool:
    if not _is_windows_platform() or not isinstance(path_text, str):
        return False
    return "\\windowsapps\\" in _normalize_windows_path(path_text)


def _probe_failure_reason(stderr_text: str, stdout_text: str) -> tuple[str, str]:
    merged = "\n".join(value for value in (stderr_text, stdout_text) if value).strip()
    lowered = merged.lower()
    if "encodings" in lowered and (
        "modulenotfounderror" in lowered or "no module named" in lowered
    ):
        return "missing_stdlib", merged
    if "access is denied" in lowered or "permission denied" in lowered:
        return "access_denied", merged
    if "the system cannot find the file specified" in lowered:
        return "not_found", merged
    return "runtime_probe_failed", merged


def _windows_where_all(command: str, *, timeout_seconds: float = 2.0) -> list[str]:
    if not _is_windows_platform():
        return []
    try:
        proc = subprocess.run(
            ["where", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            out.append(candidate)
    return out


def _venv_python_path(venv_dir: Path) -> Path:
    if _is_windows_platform():
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


@dataclass(frozen=True)
class PythonRuntimeCandidate:
    source: str
    path: str
    present: bool
    usable: bool
    reason_code: str | None = None
    reason: str | None = None
    version: str | None = None
    executable: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "present": self.present,
            "usable": self.usable,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "version": self.version,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class PythonRuntimeSelection:
    selected: PythonRuntimeCandidate | None
    candidates: tuple[PythonRuntimeCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": (self.selected.to_dict() if self.selected is not None else None),
            "candidates": [c.to_dict() for c in self.candidates],
            "rejected": [c.to_dict() for c in self.candidates if not c.usable],
        }


def _probe_python_executable(
    path_text: str,
    *,
    timeout_seconds: float,
    source: str,
) -> PythonRuntimeCandidate:
    raw = str(path_text or "").strip()
    if not raw:
        return PythonRuntimeCandidate(
            source=source,
            path="",
            present=False,
            usable=False,
            reason_code="not_found",
            reason="Empty interpreter path.",
        )

    p = Path(raw)
    present = p.exists()
    if not present:
        return PythonRuntimeCandidate(
            source=source,
            path=raw,
            present=False,
            usable=False,
            reason_code="not_found",
            reason=f"Interpreter not found at: {raw}",
        )

    if _is_windowsapps_alias(raw):
        return PythonRuntimeCandidate(
            source=source,
            path=raw,
            present=True,
            usable=False,
            reason_code="windowsapps_alias",
            reason=(
                "Resolved to a WindowsApps launcher alias. "
                "Install/select a full Python interpreter and retry."
            ),
        )

    try:
        proc = subprocess.run(
            [raw, "-c", _PYTHON_HEALTH_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PythonRuntimeCandidate(
            source=source,
            path=raw,
            present=True,
            usable=False,
            reason_code="timeout",
            reason=(
                "Interpreter health probe timed out. "
                "The interpreter may be a launcher shim or broken runtime."
            ),
        )
    except OSError as exc:
        return PythonRuntimeCandidate(
            source=source,
            path=raw,
            present=True,
            usable=False,
            reason_code="launch_failed",
            reason=str(exc),
        )

    if proc.returncode != 0:
        reason_code, reason = _probe_failure_reason(proc.stderr.strip(), proc.stdout.strip())
        return PythonRuntimeCandidate(
            source=source,
            path=raw,
            present=True,
            usable=False,
            reason_code=reason_code,
            reason=reason or f"Interpreter probe exited with code {proc.returncode}.",
        )

    payload: dict[str, Any] | None = None
    for line in reversed((proc.stdout or "").splitlines()):
        candidate_line = line.strip()
        if not candidate_line:
            continue
        try:
            decoded = json.loads(candidate_line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break

    if payload is None:
        return PythonRuntimeCandidate(
            source=source,
            path=raw,
            present=True,
            usable=False,
            reason_code="runtime_probe_failed",
            reason="Interpreter probe did not emit parseable JSON payload.",
        )

    executable = payload.get("executable")
    version = payload.get("version")
    return PythonRuntimeCandidate(
        source=source,
        path=raw,
        present=True,
        usable=True,
        executable=executable if isinstance(executable, str) else None,
        version=version if isinstance(version, str) else None,
    )


def select_python_runtime(
    *,
    workspace_dir: Path,
    timeout_seconds: float = 5.0,
    include_where_fallbacks: bool = True,
) -> PythonRuntimeSelection:
    """
    Resolve a usable Python executable path without relying on WindowsApps aliases.

    Preference order:
    - workspace `.venv` (created by runner pip-bootstrap and common local workflows)
    - active `VIRTUAL_ENV`
    - alternate PATH matches (Windows `where python`) when `python` resolves to WindowsApps alias
    - PATH commands (`py`, `python`, `python3`)
    - `sys.executable` (last resort; the runner itself is running under it)
    """

    candidates: list[PythonRuntimeCandidate] = []
    seen: set[str] = set()

    def _add(path_text: str | None, *, source: str) -> None:
        raw = str(path_text or "").strip()
        if not raw:
            return
        key = raw.lower() if _is_windows_platform() else raw
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            _probe_python_executable(raw, timeout_seconds=timeout_seconds, source=source)
        )

    workspace_venv = workspace_dir / ".venv"
    _add(str(_venv_python_path(workspace_venv)), source="workspace_venv")

    venv_env = os.environ.get("VIRTUAL_ENV", "").strip()
    if venv_env:
        _add(str(_venv_python_path(Path(venv_env))), source="virtual_env")

    python_which = shutil.which("python")
    if include_where_fallbacks and _is_windowsapps_alias(python_which):
        for entry in _windows_where_all("python"):
            if _is_windowsapps_alias(entry):
                continue
            _add(entry, source="where_python")

    _add(shutil.which("py"), source="command_py")
    _add(python_which, source="command_python")
    _add(shutil.which("python3"), source="command_python3")

    _add(sys.executable, source="sys_executable")

    selected: PythonRuntimeCandidate | None = None
    for candidate in candidates:
        if candidate.usable:
            selected = candidate
            break

    return PythonRuntimeSelection(selected=selected, candidates=tuple(candidates))


def probe_pytest_module(
    *,
    python_executable: str,
    cwd: Path,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """
    Probe `python -m pytest --version`, capturing stdout/stderr for actionable diagnostics.
    """

    argv = [python_executable, "-m", "pytest", "--version"]
    stdout_text = ""
    stderr_text = ""
    exit_code = 0
    timed_out = False
    exception: str | None = None

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
        )
        exit_code = int(proc.returncode or 0)
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        if isinstance(exc.stdout, bytes):
            stdout_text = exc.stdout.decode("utf-8", "replace")
        else:
            stdout_text = exc.stdout or ""
        if isinstance(exc.stderr, bytes):
            stderr_text = exc.stderr.decode("utf-8", "replace")
        else:
            stderr_text = exc.stderr or ""
    except OSError as exc:
        exception = str(exc)
        exit_code = 1

    merged = "\n".join(value for value in (stderr_text, stdout_text, exception) if value).strip()
    lowered = merged.lower()
    reason_code: str | None = None
    remediation: str | None = None

    if timed_out:
        reason_code = "timeout"
        remediation = "The interpreter or pytest import is hanging. Verify the selected Python."
    elif exception is not None:
        reason_code = "launch_failed"
        remediation = (
            "Python executable could not be launched. "
            "Install/select a full CPython interpreter (not WindowsApps alias), then retry."
        )
    elif exit_code != 0:
        if (
            "no module named pytest" in lowered
            or ("modulenotfounderror" in lowered and "pytest" in lowered)
        ):
            reason_code = "pytest_missing"
            remediation = (
                "Install pytest into the selected interpreter: "
                f"{python_executable} -m pip install -U pytest"
            )
        elif "access is denied" in lowered or "permission denied" in lowered:
            reason_code = "access_denied"
            remediation = (
                "The selected interpreter cannot be spawned in this environment. "
                "Avoid WindowsApps aliases; install CPython and ensure it is executable."
            )
        else:
            reason_code = "pytest_probe_failed"
            remediation = "Inspect stdout/stderr and ensure pytest is installed and importable."

    def _tail(text: str, *, max_chars: int = 2000) -> str:
        cleaned = (text or "").strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[-max_chars:]

    return {
        "command": "python -m pytest --version",
        "argv": argv,
        "python_executable": python_executable,
        "cwd": str(cwd),
        "passed": bool(exit_code == 0 and not timed_out and exception is None),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "reason_code": reason_code,
        "remediation": remediation,
        "stdout_tail": _tail(stdout_text),
        "stderr_tail": _tail(stderr_text),
        "exception": exception,
    }


def probe_pip_module(
    *,
    python_executable: str,
    cwd: Path,
    timeout_seconds: float = 6.0,
) -> dict[str, Any]:
    """
    Probe `python -m pip --version`, capturing stdout/stderr for actionable diagnostics.
    """

    argv = [python_executable, "-m", "pip", "--version"]
    stdout_text = ""
    stderr_text = ""
    exit_code = 0
    timed_out = False
    exception: str | None = None

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
        )
        exit_code = int(proc.returncode or 0)
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        if isinstance(exc.stdout, bytes):
            stdout_text = exc.stdout.decode("utf-8", "replace")
        else:
            stdout_text = exc.stdout or ""
        if isinstance(exc.stderr, bytes):
            stderr_text = exc.stderr.decode("utf-8", "replace")
        else:
            stderr_text = exc.stderr or ""
    except OSError as exc:
        exception = str(exc)
        exit_code = 1

    merged = "\n".join(value for value in (stderr_text, stdout_text, exception) if value).strip()
    lowered = merged.lower()
    reason_code: str | None = None
    remediation: str | None = None

    if timed_out:
        reason_code = "timeout"
        remediation = "The interpreter or pip import is hanging. Verify the selected Python."
    elif exception is not None:
        reason_code = "launch_failed"
        remediation = (
            "Python executable could not be launched. "
            "Install/select a full CPython interpreter (not WindowsApps alias), then retry."
        )
    elif exit_code != 0:
        if "no module named pip" in lowered or ("modulenotfounderror" in lowered and "pip" in lowered):
            reason_code = "pip_missing"
            remediation = (
                "Bootstrap pip for this interpreter (try): "
                f"{python_executable} -m ensurepip --upgrade"
            )
        elif "access is denied" in lowered or "permission denied" in lowered:
            reason_code = "access_denied"
            remediation = (
                "The selected interpreter cannot be spawned in this environment. "
                "Avoid WindowsApps aliases; install CPython and ensure it is executable."
            )
        else:
            reason_code = "pip_probe_failed"
            remediation = "Inspect stdout/stderr and ensure pip is available for this interpreter."

    def _tail(text: str, *, max_chars: int = 2000) -> str:
        cleaned = (text or "").strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[-max_chars:]

    return {
        "command": "python -m pip --version",
        "argv": argv,
        "python_executable": python_executable,
        "cwd": str(cwd),
        "passed": bool(exit_code == 0 and not timed_out and exception is None),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "reason_code": reason_code,
        "remediation": remediation,
        "stdout_tail": _tail(stdout_text),
        "stderr_tail": _tail(stderr_text),
        "exception": exception,
    }


_VERIFICATION_PYTEST_CMD_PATTERN = re.compile(r"^(?:&\s*)?pytest(\s|$)", re.IGNORECASE)
_VERIFICATION_PYTEST_MODULE_PATTERN = re.compile(r"(?:^|\s)-m\s+pytest(?:\s|$)", re.IGNORECASE)
_VERIFICATION_INSTALL_PATTERN = re.compile(
    r"\b(pip|pdm|poetry|uv)\b.*\binstall\b",
    re.IGNORECASE,
)


def verification_commands_need_pytest(commands: tuple[str, ...]) -> bool:
    for raw in commands:
        if not isinstance(raw, str) or not raw.strip():
            continue
        stripped = raw.strip()
        if _VERIFICATION_PYTEST_CMD_PATTERN.search(stripped):
            return True
        if _VERIFICATION_PYTEST_MODULE_PATTERN.search(stripped):
            return True
    return False


def verification_commands_may_provision_pytest(commands: tuple[str, ...]) -> bool:
    """
    Heuristic: treat dependency-install verification steps as provisioning, so `pytest` may
    become available later in the verification sequence.
    """

    for raw in commands:
        if not isinstance(raw, str) or not raw.strip():
            continue
        if _VERIFICATION_INSTALL_PATTERN.search(raw):
            return True
    return False
