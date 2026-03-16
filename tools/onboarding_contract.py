from __future__ import annotations

from dataclasses import dataclass

CANONICAL_NEWCOMER_LABEL = "canonical newcomer-first path"
SECONDARY_SMOKE_LABEL = "secondary developer sanity check"


@dataclass(frozen=True)
class ShellCommandPair:
    windows: str
    posix: str

    def remediation_block(self, *, intro: str = "Quick fix (recommended): from repo root, run ONE of:") -> str:
        return (
            f"{intro}\n"
            f"  - Windows PowerShell: `{self.windows}`\n"
            f"  - macOS/Linux: `{self.posix}`"
        )


OFFLINE_FIRST_SUCCESS = ShellCommandPair(
    windows=r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1",
    posix="bash ./scripts/offline_first_success.sh",
)

DEVELOPER_SMOKE = ShellCommandPair(
    windows=r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1",
    posix="bash ./scripts/smoke.sh",
)

SET_PYTHONPATH = ShellCommandPair(
    windows=r". .\scripts\set_pythonpath.ps1",
    posix="source scripts/set_pythonpath.sh",
)

FIRST_REAL_RUN_CONSOLE = (
    'usertest run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex '
    '--policy write --persona-id quickstart_sprinter --mission-id first_output_smoke'
)

FIRST_REAL_RUN_MODULE = (
    'python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex '
    '--policy write --persona-id quickstart_sprinter --mission-id first_output_smoke'
)

READ_ONLY_FIRST_RUN_CONSOLE = (
    'usertest run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex '
    '--policy inspect --persona-id quickstart_sprinter --mission-id privacy_locked_run'
)

READ_ONLY_FIRST_RUN_MODULE = (
    'python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex '
    '--policy inspect --persona-id quickstart_sprinter --mission-id privacy_locked_run'
)

PRIMARY_FIRST_RUN_POLICY_NOTE = (
    "The built-in `first_output_smoke` mission requires shell commands and edits, "
    "so the canonical first real run uses `--policy write`."
)

READ_ONLY_ALTERNATIVE_NOTE = (
    "Use `privacy_locked_run` with `--policy inspect` when you want a read-only first probe instead."
)


def one_command_first_success_remediation() -> str:
    return OFFLINE_FIRST_SUCCESS.remediation_block()
