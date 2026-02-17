from __future__ import annotations

import sys
from pathlib import Path


_CHANGE_SURFACE_KIND_ENUM: tuple[str, ...] = (
    "new_command",
    "new_flag",
    "docs_change",
    "behavior_change",
    "breaking_change",
    "new_top_level_mode",
    "new_config_schema",
    "new_api",
    "unknown",
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def lint_labeler_prompt_enums(*, repo_root: Path) -> list[str]:
    """
    Ensure the labeler prompt contains the exact surface-kind enum list.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    path = repo_root / "configs" / "backlog_prompts" / "labeler.md"
    if not path.exists():
        return [f"missing_prompt: {path}"]
    text = _read_text(path)

    missing = [kind for kind in _CHANGE_SURFACE_KIND_ENUM if f"- {kind}" not in text]
    errors: list[str] = []
    if missing:
        errors.append(f"labeler_prompt_missing_kinds: {path}: " + ", ".join(missing))

    if "Output MUST be a single valid JSON object" not in text and "Return JSON only" not in text:
        errors.append(f"labeler_prompt_missing_json_only_rule: {path}")

    return errors


def lint_miner_prompt_mentions_labeler(*, repo_root: Path) -> list[str]:
    """
    Ensure miner prompts acknowledge the labeler stage (anti-command-sprawl guardrail).

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    path = repo_root / "configs" / "backlog_prompts" / "miner_default.md"
    if not path.exists():
        return [f"missing_prompt: {path}"]
    text = _read_text(path).lower()

    errors: list[str] = []
    if "change_surface" not in text:
        errors.append(f"miner_prompt_missing_change_surface_note: {path}")
    if "labeler" not in text:
        errors.append(f"miner_prompt_missing_labeler_note: {path}")
    return errors


def main(argv: list[str] | None = None) -> int:
    """
    CLI entrypoint.

    Run from repo root:

        python tools/lint_prompts.py
    """

    _ = argv
    repo_root = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    errors.extend(lint_labeler_prompt_enums(repo_root=repo_root))
    errors.extend(lint_miner_prompt_mentions_labeler(repo_root=repo_root))
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
