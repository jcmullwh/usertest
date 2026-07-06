from __future__ import annotations

from pathlib import Path

from runner_core.remote_effects import (
    FIRST_USE_REMOTE_EFFECT_COMMANDS,
    REMOTE_EFFECTS,
    REMOTE_EFFECTS_BY_COMMAND,
    first_use_remote_effects,
    load_implement_settings_defaults,
    render_remote_effects_markdown_table,
    validate_remote_effects_contract,
)

RELEVANT_COMMANDS = {
    "usertest run",
    "usertest batch",
    "usertest report",
    "usertest matrix plan",
    "usertest matrix run",
    "usertest lint",
    "usertest reports compile",
    "usertest reports analyze",
    "usertest token-monitor analyze",
    "usertest token-monitor batch-context",
    "usertest init-usertest",
    "usertest personas list",
    "usertest missions list",
    "usertest-backlog reports compile",
    "usertest-backlog reports analyze",
    "usertest-backlog reports window",
    "usertest-backlog reports intent-snapshot",
    "usertest-backlog reports review-ux",
    "usertest-backlog reports sync-atom-actions",
    "usertest-backlog reports backlog",
    "usertest-backlog reports export-tickets",
    "usertest-backlog triage-prs",
    "usertest-backlog triage-backlog",
    "usertest-backlog triage-atoms",
    "usertest-implement run",
    "usertest-implement review run",
    "usertest-implement review status",
    "usertest-implement review merge",
    "usertest-implement reports summarize",
    "usertest-implement tickets list",
    "usertest-implement tickets next",
    "usertest-implement tickets run-next",
    "usertest-implement tickets move",
    "usertest-implement tickets discard",
    "usertest-implement batch run",
    "usertest-implement batch status",
    "usertest-implement batch recover",
    "usertest-implement maintenance-images list",
    "usertest-implement maintenance-images cleanup",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _docs_remote_effects_table() -> str:
    text = (_repo_root() / "docs" / "tutorials" / "getting-started.md").read_text(
        encoding="utf-8"
    )
    begin = "<!-- BEGIN REMOTE_EFFECTS_TABLE -->"
    end = "<!-- END REMOTE_EFFECTS_TABLE -->"
    assert begin in text
    assert end in text
    return text.split(begin, 1)[1].split(end, 1)[0].strip()


def test_remote_effects_contract_is_complete_and_consistent() -> None:
    assert set(REMOTE_EFFECTS_BY_COMMAND) == RELEVANT_COMMANDS
    assert len(REMOTE_EFFECTS) == len(REMOTE_EFFECTS_BY_COMMAND)
    assert validate_remote_effects_contract() == []


def test_first_use_docs_table_is_generated_from_contract() -> None:
    assert set(FIRST_USE_REMOTE_EFFECT_COMMANDS).issubset(REMOTE_EFFECTS_BY_COMMAND)
    expected = render_remote_effects_markdown_table(first_use_remote_effects())
    assert _docs_remote_effects_table() == expected


def test_implementation_settings_defaults_match_remote_write_contract() -> None:
    defaults = load_implement_settings_defaults(
        _repo_root() / "configs" / "usertest_implement_settings.yaml"
    )
    assert defaults["commit"] is True
    assert defaults["push"] is True
    assert defaults["pr"] is True

    run = REMOTE_EFFECTS_BY_COMMAND["usertest-implement run"]
    run_next = REMOTE_EFFECTS_BY_COMMAND["usertest-implement tickets run-next"]
    for effect in (run, run_next):
        assert effect.boundary == "remote-write"
        assert effect.commits.by_default
        assert effect.pushes.by_default
        assert effect.pull_requests.by_default
        assert effect.defaults_source is not None

    review_run = REMOTE_EFFECTS_BY_COMMAND["usertest-implement review run"]
    assert review_run.boundary == "remote-write"
    assert review_run.pull_requests.by_default


def test_run_next_dry_run_discloses_refresh_behavior() -> None:
    run_next = REMOTE_EFFECTS_BY_COMMAND["usertest-implement tickets run-next"]
    dry_run = next(mod for mod in run_next.modifiers if mod.name == "--dry-run")
    assert "--no-refresh-backlog" in dry_run.effect


def test_existing_dry_run_and_print_modifiers_are_classified() -> None:
    expected_modifiers = {
        "usertest batch": {"--validate-only", "--print-requests"},
        "usertest-backlog reports backlog": {"--dry-run"},
        "usertest-backlog reports review-ux": {"--dry-run"},
        "usertest-backlog reports sync-atom-actions": {"--dry-run"},
        "usertest token-monitor analyze": {"--no-write"},
        "usertest-implement run": {"--dry-run", "--no-commit", "--no-push", "--no-pr"},
        "usertest-implement review run": {"--dry-run"},
        "usertest-implement tickets run-next": {
            "--dry-run",
            "--no-refresh-backlog",
            "--no-commit/--no-push/--no-pr",
        },
        "usertest-implement tickets move": {"--dry-run"},
        "usertest-implement maintenance-images cleanup": {"--dry-run", "--apply"},
    }
    for command, expected in expected_modifiers.items():
        names = {modifier.name for modifier in REMOTE_EFFECTS_BY_COMMAND[command].modifiers}
        assert expected.issubset(names)
