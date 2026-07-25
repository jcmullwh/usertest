from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

_SHELL_CONTROL_TOKENS: frozenset[str] = frozenset(
    {
        "|",
        "||",
        "&&",
        ";",
        "<",
        ">",
        ">>",
        "2>",
        "2>>",
        "1>",
        "1>>",
        "&>",
    }
)
_BASH_SPECIFIC_SNIPPETS: tuple[str, ...] = ("$(", "<<", "[[", "]]")
_BASH_SPECIFIC_COMMANDS: frozenset[str] = frozenset({"export", "source", ".", "unset"})
_BASH_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_LINE_INSPECTION_RE = re.compile(
    r"^\s*nl(?:\s+-ba)?\s+(?P<path>.+?)\s*\|\s*sed\s+-n\s+"
    r"(?P<quote>['\"]?)(?P<start>\d+),(?P<end>\d+)p(?P=quote)\s*$"
)


@dataclass(frozen=True)
class ShellCommandNormalization:
    action: Literal["passthrough", "rewrite", "blocked"]
    command: str
    kind: str | None = None
    reason: str | None = None
    hint: str | None = None


def _split_command(command: str, *, prefer_posix: bool) -> list[str]:
    posix_order = (True, False) if prefer_posix else (False, True)
    for posix in posix_order:
        try:
            return shlex.split(command, posix=posix)
        except ValueError:
            continue
    return command.split()


def _unwrap_shell_wrapper(command: str) -> tuple[str, str] | None:
    argv = _split_command(command, prefer_posix=True)
    if len(argv) < 3:
        return None

    exe = str(argv[0] or "").replace("\\", "/").strip().lower()
    if not exe:
        return None
    base = exe.rsplit("/", 1)[-1]

    if base in {"bash", "sh"} and argv[1] in {"-lc", "-c"}:
        inner = argv[2]
        return ("bash", inner.strip()) if isinstance(inner, str) and inner.strip() else None

    if base in {"cmd", "cmd.exe"} and argv[1].lower() == "/c":
        inner = argv[2]
        return ("cmd", inner.strip()) if isinstance(inner, str) and inner.strip() else None

    if base in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"} and argv[1].lower() in {
        "-command",
        "-c",
    }:
        inner = argv[2]
        return ("powershell", inner.strip()) if isinstance(inner, str) and inner.strip() else None

    return None


def _powershell_quote_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _rewrite_line_inspection_to_powershell(command: str) -> ShellCommandNormalization | None:
    match = _LINE_INSPECTION_RE.match(command)
    if match is None:
        return None

    path_expr = match.group("path").strip()
    try:
        path_tokens = shlex.split(path_expr, posix=True)
    except ValueError:
        return None
    if len(path_tokens) != 1:
        return None

    start = int(match.group("start"))
    end = int(match.group("end"))
    if start <= 0 or end < start:
        return None

    rewritten = (
        f"$i=1; Get-Content -LiteralPath {_powershell_quote_literal(path_tokens[0])} | % "
        "{ if ($i -ge "
        f"{start} -and $i -le {end}) {{ '{{0,6}}: {{1}}' -f $i, $_ }}; $i++ }}"
    )
    return ShellCommandNormalization(
        action="rewrite",
        command=rewritten,
        kind="unix_line_inspection_to_powershell",
        reason=(
            "The command uses `nl | sed` line inspection, which is not portable to the "
            "PowerShell shell contract."
        ),
        hint=(
            "Use `Get-Content -LiteralPath ...` with explicit line numbering when targeting "
            "PowerShell."
        ),
    )


def _normalize_bash_wrapper_for_powershell(command: str) -> ShellCommandNormalization | None:
    wrapper = _unwrap_shell_wrapper(command)
    if wrapper is None:
        return None

    wrapper_kind, inner = wrapper
    if wrapper_kind != "bash":
        return None

    line_rewrite = _rewrite_line_inspection_to_powershell(inner)
    if line_rewrite is not None:
        return line_rewrite

    tokens = _split_command(inner, prefer_posix=True)
    if not tokens:
        return ShellCommandNormalization(
            action="blocked",
            command=command.strip(),
            kind="powershell_unsupported_bash_wrapper",
            reason="The bash wrapper did not contain a usable inner command.",
            hint="Run the intended command directly in PowerShell, or remove the empty wrapper.",
        )

    if any(token in _SHELL_CONTROL_TOKENS for token in tokens):
        return ShellCommandNormalization(
            action="blocked",
            command=command.strip(),
            kind="powershell_unsupported_bash_wrapper",
            reason=(
                "The command is wrapped in `bash -lc` / `sh -lc` and still relies on shell "
                "operators that are not safely portable to PowerShell."
            ),
            hint=(
                "Run shell-neutral commands directly, or rewrite the logic with PowerShell-native "
                "sequencing and `$LASTEXITCODE` checks."
            ),
        )

    first = tokens[0]
    if (
        first in _BASH_SPECIFIC_COMMANDS
        or _BASH_ASSIGNMENT_RE.match(first) is not None
        or any(snippet in inner for snippet in _BASH_SPECIFIC_SNIPPETS)
    ):
        return ShellCommandNormalization(
            action="blocked",
            command=command.strip(),
            kind="powershell_unsupported_bash_wrapper",
            reason=(
                "The command is wrapped in `bash -lc` / `sh -lc` and uses bash-specific syntax "
                "that does not have a safe automatic PowerShell translation."
            ),
            hint=(
                "Rewrite the command in PowerShell-native syntax, or stop and report the "
                "portability issue instead of guessing."
            ),
        )

    return ShellCommandNormalization(
        action="rewrite",
        command=inner,
        kind="bash_wrapper_unwrapped_for_host_shell",
        reason=(
            "The command was wrapped in `bash -lc` / `sh -lc` even though the active shell is "
            "PowerShell."
        ),
        hint="Run shell-neutral commands directly in the active shell.",
    )


def normalize_command_for_shell(
    command: str,
    *,
    shell_family: str,
) -> ShellCommandNormalization:
    raw = (command or "").strip()
    if not raw:
        return ShellCommandNormalization(action="passthrough", command="")

    normalized_shell = (shell_family or "").strip().lower() or "bash"
    if normalized_shell == "powershell":
        bash_wrapper = _normalize_bash_wrapper_for_powershell(raw)
        if bash_wrapper is not None:
            return bash_wrapper

        line_rewrite = _rewrite_line_inspection_to_powershell(raw)
        if line_rewrite is not None:
            return line_rewrite

    return ShellCommandNormalization(action="passthrough", command=raw)


def render_shell_command_guidance_md(*, shell_family: str) -> str:
    normalized_shell = (shell_family or "").strip().lower() or "bash"
    bullets: list[str] = []

    if normalized_shell == "powershell":
        bullets.extend(
            [
                (
                    "- PowerShell (Windows): do not wrap commands in `bash -lc` / "
                    "`sh -lc`. If the inner command is shell-neutral (for example "
                    "`python -m pytest -q`), run it directly. If it relies on "
                    "bash-specific operators or syntax, stop and report the portability "
                    "issue."
                ),
                (
                    "- PowerShell (Windows): rewrite Unix-only line inspection like "
                    "`nl -ba path | sed -n '5,12p'` to `$i=1; Get-Content -LiteralPath "
                    "path | % { if ($i -ge 5 -and $i -le 12) { '{0,6}: {1}' -f $i, $_ "
                    "}; $i++ }`."
                ),
            ]
        )
    else:
        bullets.append(
            "- bash/sh: run shell-native commands directly; avoid redundant "
            "`bash -lc` wrappers unless the environment explicitly requires them."
        )

    bullets.append(
        "- If no safe translation exists for the active shell, stop and report "
        "the portability issue instead of guessing."
    )

    return "\n".join(bullets)
