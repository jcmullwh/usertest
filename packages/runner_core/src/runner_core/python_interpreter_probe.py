from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PYTHON_COMMANDS: tuple[str, ...] = ("python", "python3", "py")

_PYTHON_HEALTH_PROBE = (
    "import encodings, json, sys; "
    "print(json.dumps({'executable': sys.executable, 'version': sys.version.split()[0]}))"
)


def _coerce_commands(raw: Sequence[str] | None) -> list[str]:
    values = list(raw) if raw is not None else list(DEFAULT_PYTHON_COMMANDS)
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        cmd = item.strip()
        if not cmd or cmd in seen:
            continue
        out.append(cmd)
        seen.add(cmd)
    return out


def _path_matches_current_interpreter(path_text: str | None) -> bool:
    if not isinstance(path_text, str) or not path_text.strip():
        return False
    current = Path(sys.executable)
    candidate = Path(path_text)
    try:
        return current.resolve(strict=False) == candidate.resolve(strict=False)
    except OSError:
        return current.as_posix().lower() == candidate.as_posix().lower()


def _is_windows_platform(*, force_windows: bool | None = None) -> bool:
    if force_windows is not None:
        return force_windows
    return os.name == "nt"


def _is_windowsapps_alias(path_text: str | None, *, is_windows: bool) -> bool:
    if not is_windows or not isinstance(path_text, str):
        return False
    normalized = path_text.replace("/", "\\").lower()
    return "\\windowsapps\\" in normalized


def _probe_failure_reason(stderr_text: str, stdout_text: str) -> tuple[str, str]:
    merged = "\n".join(value for value in (stderr_text, stdout_text) if value).strip()
    lowered = merged.lower()
    if "encodings" in lowered and (
        "modulenotfounderror" in lowered or "no module named" in lowered
    ):
        return "missing_stdlib", merged
    if "access is denied" in lowered or "permission denied" in lowered:
        return "access_denied", merged
    return "runtime_probe_failed", merged


@dataclass(frozen=True)
class PythonCandidateProbe:
    command: str
    resolved_path: str | None
    present: bool
    usable: bool
    reason_code: str | None = None
    reason: str | None = None
    version: str | None = None
    executable: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "resolved_path": self.resolved_path,
            "present": self.present,
            "usable": self.usable,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "version": self.version,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class PythonInterpreterProbeResult:
    selected_command: str | None
    selected_resolved_path: str | None
    selected_version: str | None
    selected_executable: str | None
    candidates: tuple[PythonCandidateProbe, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": (
                {
                    "command": self.selected_command,
                    "resolved_path": self.selected_resolved_path,
                    "version": self.selected_version,
                    "executable": self.selected_executable,
                }
                if self.selected_command is not None
                else None
            ),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "rejected": [
                candidate.to_dict() for candidate in self.candidates if not candidate.usable
            ],
        }

    def by_command(self) -> dict[str, PythonCandidateProbe]:
        return {candidate.command: candidate for candidate in self.candidates}


def probe_python_interpreters(
    *,
    candidate_commands: Sequence[str] | None = None,
    timeout_seconds: float = 5.0,
    force_windows: bool | None = None,
) -> PythonInterpreterProbeResult:
    commands = _coerce_commands(candidate_commands)
    is_windows = _is_windows_platform(force_windows=force_windows)
    timeout = max(0.1, float(timeout_seconds))
    candidates: list[PythonCandidateProbe] = []

    for command in commands:
        resolved = shutil.which(command)
        if resolved is None:
            candidates.append(
                PythonCandidateProbe(
                    command=command,
                    resolved_path=None,
                    present=False,
                    usable=False,
                    reason_code="not_found",
                    reason=f"`{command}` was not found on PATH.",
                )
            )
            continue

        if _is_windowsapps_alias(resolved, is_windows=is_windows):
            candidates.append(
                PythonCandidateProbe(
                    command=command,
                    resolved_path=resolved,
                    present=True,
                    usable=False,
                    reason_code="windowsapps_alias",
                    reason=(
                        "Resolved to a WindowsApps launcher alias. "
                        "Install/select a full Python interpreter and retry."
                    ),
                )
            )
            continue

        try:
            proc = subprocess.run(
                [resolved, "-c", _PYTHON_HEALTH_PROBE],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            candidates.append(
                PythonCandidateProbe(
                    command=command,
                    resolved_path=resolved,
                    present=True,
                    usable=False,
                    reason_code="timeout",
                    reason=(
                        "Interpreter health probe timed out. "
                        "The interpreter may be a launcher shim or broken runtime."
                    ),
                )
            )
            continue
        except OSError as exc:
            candidates.append(
                PythonCandidateProbe(
                    command=command,
                    resolved_path=resolved,
                    present=True,
                    usable=False,
                    reason_code="launch_failed",
                    reason=str(exc),
                )
            )
            continue

        if proc.returncode != 0:
            reason_code, reason = _probe_failure_reason(
                stderr_text=proc.stderr.strip(),
                stdout_text=proc.stdout.strip(),
            )
            candidates.append(
                PythonCandidateProbe(
                    command=command,
                    resolved_path=resolved,
                    present=True,
                    usable=False,
                    reason_code=reason_code,
                    reason=reason or f"Interpreter probe exited with code {proc.returncode}.",
                )
            )
            continue

        payload: dict[str, Any] | None = None
        for line in reversed(proc.stdout.splitlines()):
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
            candidates.append(
                PythonCandidateProbe(
                    command=command,
                    resolved_path=resolved,
                    present=True,
                    usable=False,
                    reason_code="runtime_probe_failed",
                    reason="Interpreter probe did not emit parseable JSON payload.",
                )
            )
            continue

        executable = payload.get("executable")
        version = payload.get("version")
        candidates.append(
            PythonCandidateProbe(
                command=command,
                resolved_path=resolved,
                present=True,
                usable=True,
                executable=executable if isinstance(executable, str) else None,
                version=version if isinstance(version, str) else None,
            )
        )

    usable = [candidate for candidate in candidates if candidate.usable]
    usable.sort(
        key=lambda candidate: (
            0 if _path_matches_current_interpreter(candidate.resolved_path) else 1,
            0 if candidate.command == "python" else 1 if candidate.command == "python3" else 2,
            candidate.command,
        )
    )
    selected = usable[0] if usable else None

    return PythonInterpreterProbeResult(
        selected_command=selected.command if selected is not None else None,
        selected_resolved_path=selected.resolved_path if selected is not None else None,
        selected_version=selected.version if selected is not None else None,
        selected_executable=selected.executable if selected is not None else None,
        candidates=tuple(candidates),
    )
