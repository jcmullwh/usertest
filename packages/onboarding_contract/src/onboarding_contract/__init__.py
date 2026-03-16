from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ShellId = Literal["windows_powershell", "posix_bash"]
PathId = Literal["offline_first_success", "doctor", "smoke"]


@dataclass(frozen=True)
class ShellCommand:
    shell_id: ShellId
    label: str
    command: str
    fence_language: str


@dataclass(frozen=True)
class CommandPath:
    id: PathId
    title: str
    precedence: str
    purpose: str
    commands: tuple[ShellCommand, ...]
    success_signal: str | None = None
    notes: tuple[str, ...] = ()

    def command_for(self, shell_id: ShellId) -> str:
        for command in self.commands:
            if command.shell_id == shell_id:
                return command.command
        raise KeyError(f"Unsupported shell_id for {self.id}: {shell_id}")


@dataclass(frozen=True)
class AlternateEntrypoint:
    id: str
    title: str
    precedence: str
    when_to_use: str


@dataclass(frozen=True)
class FirstRealRun:
    purpose: str
    preferred_invocation: str
    preferred_command: str
    alternate_invocation: str
    alternate_command: str
    default_policy: str
    persona_id: str
    mission_id: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShellFallbackRule:
    when: str
    guidance: str


@dataclass(frozen=True)
class OnboardingContract:
    canonical_path: CommandPath
    alternate_entrypoints: tuple[AlternateEntrypoint, ...]
    first_real_run: FirstRealRun
    shell_fallback_ladder: tuple[ShellFallbackRule, ...]

    def command_path(self, path_id: PathId) -> CommandPath:
        paths = {
            self.canonical_path.id: self.canonical_path,
            "doctor": next(
                entry
                for entry in _COMMAND_PATHS
                if entry.id == "doctor"
            ),
            "smoke": next(
                entry
                for entry in _COMMAND_PATHS
                if entry.id == "smoke"
            ),
        }
        return paths[path_id]


_OFFLINE_FIRST_SUCCESS = CommandPath(
    id="offline_first_success",
    title="Offline-safe first success",
    precedence="canonical newcomer-first path",
    purpose=(
        "Use this from repo root as the single newcomer-first onboarding command. "
        "It creates a local .venv, installs minimal deps, sets PYTHONPATH, and re-renders "
        "the golden fixture without calling agents or network services."
    ),
    commands=(
        ShellCommand(
            shell_id="windows_powershell",
            label="Windows PowerShell",
            command=(
                r"powershell -NoProfile -ExecutionPolicy Bypass -File "
                r".\scripts\offline_first_success.ps1"
            ),
            fence_language="powershell",
        ),
        ShellCommand(
            shell_id="posix_bash",
            label="macOS / Linux",
            command="bash ./scripts/offline_first_success.sh",
            fence_language="bash",
        ),
    ),
    success_signal=(
        'prints a "Scratch run dir" path containing a freshly re-rendered `report.md`.'
    ),
    notes=("no agents", "no network calls"),
)

_DOCTOR = CommandPath(
    id="doctor",
    title="Doctor",
    precedence="diagnostic",
    purpose=(
        "Use this when you want a PASS/FAIL environment check and copy/paste remediation "
        "before or after the canonical newcomer-first path."
    ),
    commands=(
        ShellCommand(
            shell_id="windows_powershell",
            label="Windows PowerShell",
            command=r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor.ps1",
            fence_language="powershell",
        ),
        ShellCommand(
            shell_id="posix_bash",
            label="macOS / Linux",
            command="bash ./scripts/doctor.sh",
            fence_language="bash",
        ),
    ),
)

_SMOKE = CommandPath(
    id="smoke",
    title="Developer smoke",
    precedence="secondary",
    purpose=(
        "Use this after the canonical newcomer-first path when you want a deterministic "
        "end-to-end sanity check (doctor -> deps -> CLI help -> smoke tests)."
    ),
    commands=(
        ShellCommand(
            shell_id="windows_powershell",
            label="Windows PowerShell",
            command=r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1",
            fence_language="powershell",
        ),
        ShellCommand(
            shell_id="posix_bash",
            label="macOS / Linux",
            command="bash ./scripts/smoke.sh",
            fence_language="bash",
        ),
    ),
)

_COMMAND_PATHS = (_OFFLINE_FIRST_SUCCESS, _DOCTOR, _SMOKE)

ONBOARDING_CONTRACT = OnboardingContract(
    canonical_path=_OFFLINE_FIRST_SUCCESS,
    alternate_entrypoints=(
        AlternateEntrypoint(
            id=_DOCTOR.id,
            title=_DOCTOR.title,
            precedence=_DOCTOR.precedence,
            when_to_use=_DOCTOR.purpose,
        ),
        AlternateEntrypoint(
            id=_SMOKE.id,
            title=_SMOKE.title,
            precedence=_SMOKE.precedence,
            when_to_use=_SMOKE.purpose,
        ),
    ),
    first_real_run=FirstRealRun(
        purpose=(
            "Use this after the environment is working and an agent CLI is installed and "
            "authenticated."
        ),
        preferred_invocation="console script",
        preferred_command=(
            'usertest run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex '
            "--policy write --persona-id quickstart_sprinter --mission-id first_output_smoke"
        ),
        alternate_invocation="module invocation",
        alternate_command=(
            'python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex '
            "--policy write --persona-id quickstart_sprinter --mission-id first_output_smoke"
        ),
        default_policy="write",
        persona_id="quickstart_sprinter",
        mission_id="first_output_smoke",
        notes=(
            "The built-in first_output_smoke mission requires edits, so the newcomer-first real "
            "run uses --policy write.",
            "If you only want a read-only probe, switch to a non-edit mission such as "
            "privacy_locked_run and use --policy inspect.",
        ),
    ),
    shell_fallback_ladder=(
        ShellFallbackRule(
            when="Windows",
            guidance="Prefer the PowerShell wrappers for newcomer-first commands on Windows.",
        ),
        ShellFallbackRule(
            when="macOS/Linux",
            guidance="Use the bash wrappers for newcomer-first commands on macOS/Linux.",
        ),
        ShellFallbackRule(
            when="Windows bash is on PATH but blocked",
            guidance=(
                "Use the PowerShell wrappers and avoid bash-based validation steps in that "
                "environment."
            ),
        ),
    ),
)


def command_path(path_id: PathId) -> CommandPath:
    return ONBOARDING_CONTRACT.command_path(path_id)


def render_first_success_remediation() -> str:
    path = ONBOARDING_CONTRACT.canonical_path
    lines = ["Quick fix (recommended): from repo root, run ONE of:"]
    lines.extend(f"  - {command.label}: `{command.command}`" for command in path.commands)
    return "\n".join(lines)


__all__ = [
    "AlternateEntrypoint",
    "CommandPath",
    "FirstRealRun",
    "ONBOARDING_CONTRACT",
    "OnboardingContract",
    "PathId",
    "ShellCommand",
    "ShellFallbackRule",
    "ShellId",
    "command_path",
    "render_first_success_remediation",
]
