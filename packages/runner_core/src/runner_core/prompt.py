from __future__ import annotations

import re
from collections.abc import Mapping


class TemplateSubstitutionError(ValueError):
    pass


_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


CANONICAL_EXECUTION_NOTES_MD = "\n".join(
    [
        "- Prefer the environment's file/directory tools for repo inspection "
        "(read/search/list) over launching shell commands when possible.",
        "- When using `run_shell_command`, use syntax compatible with the execution "
        "shell family in `environment.execution_backend.shell` (bash vs PowerShell). "
        "Example: bash `export FOO=bar`; PowerShell `$env:FOO='bar'`.",
        "- PowerShell (Windows): assume PowerShell 5.1 compatibility unless the "
        "environment explicitly says otherwise (no `&&` / `||`). Run commands "
        "separately, or check `$LASTEXITCODE` after each native command and "
        "`exit $LASTEXITCODE` on failure.",
        "- PowerShell (Windows): bash-only helpers like `nl` may be unavailable. "
        "Example line numbers: `$i=1; Get-Content -LiteralPath path | % { "
        "'{0,6}: {1}' -f $i, $_; $i++ }`",
        "- Ripgrep: when searching for a literal pattern that begins with `-`, pass "
        "`--` to end option parsing (example: `rg -n -- \"--skip-install\" README.md`).",
        "- Ripgrep: exit code `1` means \"no matches found\" (not necessarily a tool failure).",
        "- Avoid heredocs (for example `<<EOF ... EOF`) in `run_shell_command`; "
        "they may be rejected by sandbox policy. For multiline content, prefer "
        "`write_file` / `replace`.",
        "- If this looks like a Python repo and an import fails (for example `import pytest`), "
        "look for a documented setup path (`README.md`, `requirements*.txt`, `pyproject.toml`) "
        "and install the minimal deps before retrying imports.",
        "- Before inspecting a specific subpath, confirm it exists (use "
        "`environment.preflight.workspace_root_snapshot` and/or list parent directories first).",
        "- On Windows PowerShell, prefer `-LiteralPath` for paths that contain `{` or `}` "
        "(for example Cookiecutter template paths).",
        "- If command execution is blocked, record the block and consult "
        "`environment.preflight.capabilities` and `environment.preflight.command_diagnostics` "
        "for an actionable remediation path.",
    ]
).strip()


def build_prompt_from_template(*, template_text: str, variables: Mapping[str, str]) -> str:
    """
    Strict substitution: errors if the template contains placeholders not present in variables.
    """

    missing: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.add(key)
            return match.group(0)

        value = variables[key]
        if not isinstance(value, str):
            raise TemplateSubstitutionError(
                f"Template variable {key!r} must be a string, got {type(value).__name__}."
            )
        return value

    rendered = _PLACEHOLDER_RE.sub(_replace, template_text)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise TemplateSubstitutionError(f"Missing template variables: {missing_list}.")
    return rendered
