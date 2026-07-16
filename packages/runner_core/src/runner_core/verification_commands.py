"""Fail-closed validation for commands used as verification evidence."""

from __future__ import annotations

import os
import shlex
from typing import Final

_SHELL_WRAPPERS: Final = frozenset(
    {"bash", "sh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
)
_WRAPPER_FLAGS: Final = frozenset({"-c", "-lc", "/c", "-command"})
_INLINE_INTERPRETER_FLAGS: Final = {
    "node": frozenset({"-e", "--eval"}),
    "node.exe": frozenset({"-e", "--eval"}),
    "perl": frozenset({"-e"}),
    "perl.exe": frozenset({"-e"}),
    "php": frozenset({"-r"}),
    "php.exe": frozenset({"-r"}),
    "python": frozenset({"-c"}),
    "python.exe": frozenset({"-c"}),
    "python3": frozenset({"-c"}),
    "python3.exe": frozenset({"-c"}),
    "ruby": frozenset({"-e"}),
    "ruby.exe": frozenset({"-e"}),
}


def _unquoted_control_operators(command: str) -> list[str]:
    """Return shell control operators outside quoted literal arguments."""

    found: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote is not None:
            if quote != "'" and command[index : index + 2] == "$(":
                found.append("command_substitution")
                index += 2
                continue
            if quote != "'" and character == "`":
                # POSIX shells perform command substitution with backticks;
                # PowerShell uses them as an escape. Verification evidence
                # should require neither ambiguous form.
                found.append("backtick_evaluation")
                index += 1
                continue
            if character == quote:
                # PowerShell represents a literal quote inside a single-quoted
                # string by doubling it. Treat that pair as data.
                if quote == "'" and index + 1 < len(command) and command[index + 1] == quote:
                    index += 2
                    continue
                quote = None
                index += 1
                continue
            index += 1
            continue

        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if command[index : index + 2] == "$(":
            found.append("command_substitution")
            index += 2
            continue
        if character == "`":
            found.append("backtick_evaluation")
            index += 1
            continue
        if character in {"\r", "\n"}:
            found.append("newline")
            index += 1
            continue
        pair = command[index : index + 2]
        if pair in {"&&", "||", ">>", "<<"}:
            found.append(pair)
            index += 2
            continue
        if character in {";", "|", "&", "<", ">"}:
            found.append(character)
        index += 1
    return list(dict.fromkeys(found))


def _shell_wrapper_inner_command(command: str) -> str | None:
    """Extract a common shell-wrapper payload for recursive validation."""

    candidates: list[list[str]] = []
    for posix in (True, False):
        try:
            candidates.append(shlex.split(command, posix=posix))
        except ValueError:
            continue
    for argv in candidates:
        if not argv:
            continue
        executable = os.path.basename(str(argv[0]).replace("\\", "/")).casefold()
        if executable not in _SHELL_WRAPPERS:
            continue
        lowered = [str(value).casefold() for value in argv]
        for index, token in enumerate(lowered[:-1]):
            if token not in _WRAPPER_FLAGS:
                continue
            inner = str(argv[index + 1]).strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in {"'", '"'}:
                inner = inner[1:-1]
            return inner or None
    return None


def _inline_interpreter_error(command: str) -> str | None:
    """Reject unreviewable inline programs presented as verification tools."""

    candidates: list[list[str]] = []
    for posix in (True, False):
        try:
            candidates.append(shlex.split(command, posix=posix))
        except ValueError:
            continue
    for argv in candidates:
        values = [str(value) for value in argv]
        lowered = [value.casefold() for value in values]
        if len(lowered) >= 2 and lowered[:2] in (["pdm", "run"], ["uv", "run"]):
            values = values[2:]
            lowered = lowered[2:]
        if not values:
            continue
        executable = os.path.basename(values[0].replace("\\", "/")).casefold()
        forbidden_flags = _INLINE_INTERPRETER_FLAGS.get(executable)
        if forbidden_flags is None:
            continue
        for token in lowered[1:]:
            if token in forbidden_flags:
                return f"verification_command_inline_code_untrusted:{executable}:{token}"
    return None


def verification_command_safety_errors(command: str) -> list[str]:
    """Reject command composition that can hide a failing verification step.

    Verification commands are evidence, not general-purpose shell scripts. Each
    configured entry must therefore be one invocation whose exit code is the exit
    code being recorded. Callers should place independent invocations in separate
    list entries.
    """

    raw = str(command or "").strip()
    if not raw:
        return ["verification_command_empty"]
    errors = [
        f"verification_command_shell_control_operator:{operator}"
        for operator in _unquoted_control_operators(raw)
    ]
    inner = _shell_wrapper_inner_command(raw)
    if inner is not None:
        errors.extend(
            f"verification_command_nested_shell:{error}"
            for error in verification_command_safety_errors(inner)
        )
    inline_error = _inline_interpreter_error(raw)
    if inline_error is not None:
        errors.append(inline_error)
    return list(dict.fromkeys(errors))


__all__ = ["verification_command_safety_errors"]
