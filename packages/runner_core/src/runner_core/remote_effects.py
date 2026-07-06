# ruff: noqa: E501
"""Shared remote-effect classifications for the public CLI surface.

The table in this module is intentionally data-only: command behavior stays in the
individual CLIs, while docs and regression tests use this contract as the single
place to describe whether an existing command can write local artifacts, expose
sensitive run artifacts, export draft tickets, commit, push, or open/merge PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

EffectState = Literal["never", "may", "default"]
Boundary = Literal["local-only", "draft/export", "remote-write"]


@dataclass(frozen=True)
class Effect:
    """A remote-effect dimension for one command.

    ``state`` means:
    - ``never``: the command is not expected to perform this effect.
    - ``may``: the command can perform the effect when an existing flag/config/path asks for it.
    - ``default``: the effect is part of the effective default path for at least one documented use.
    """

    state: EffectState
    detail: str = ""

    @property
    def possible(self) -> bool:
        return self.state in {"may", "default"}

    @property
    def by_default(self) -> bool:
        return self.state == "default"


@dataclass(frozen=True)
class RemoteEffectModifier:
    """Existing flag/config modifier that materially changes remote-effect behavior."""

    name: str
    effect: str


@dataclass(frozen=True)
class CommandRemoteEffects:
    """Structured remote-effect boundary for one existing CLI command."""

    command: str
    boundary: Boundary
    local_artifacts: Effect
    sensitive_artifacts: Effect
    draft_exports: Effect
    commits: Effect
    pushes: Effect
    pull_requests: Effect
    summary: str
    modifiers: tuple[RemoteEffectModifier, ...] = ()
    defaults_source: str | None = None


NO_EFFECT = Effect("never")
LOCAL_ARTIFACTS = Effect("default", "Writes files under the configured repo/run/output path.")
SENSITIVE_RUN_ARTIFACTS = Effect(
    "default",
    "Run artifacts can include prompts, transcripts, tool output, target paths, or patches.",
)
SENSITIVE_DERIVED_ARTIFACTS = Effect(
    "may",
    "Derived reports/exports may include excerpts or metadata from sensitive run artifacts.",
)


REMOTE_EFFECTS: tuple[CommandRemoteEffects, ...] = (
    CommandRemoteEffects(
        command="usertest run",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_RUN_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Runs one agent evaluation and writes a local run directory; no built-in git/PR finalization.",
        modifiers=(
            RemoteEffectModifier(
                "--policy safe|inspect|write",
                "Controls agent tool permissions; it is not artifact redaction and does not make run artifacts safe to share.",
            ),
            RemoteEffectModifier(
                "--exec-network none",
                "Limits Docker container networking only; it is not an end-to-end offline/privacy boundary.",
            ),
        ),
    ),
    CommandRemoteEffects(
        command="usertest batch",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_RUN_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Runs multiple evaluations and writes local run directories; no built-in git/PR finalization.",
        modifiers=(
            RemoteEffectModifier(
                "--validate-only",
                "Validates targets/configuration and exits before agent execution.",
            ),
            RemoteEffectModifier(
                "--print-requests",
                "Prints resolved requests as JSON and exits without executing agents.",
            ),
        ),
    ),
    CommandRemoteEffects(
        command="usertest report",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Re-renders report files for an existing local run directory.",
    ),
    CommandRemoteEffects(
        command="usertest matrix plan",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=Effect("may", "Expanded matrix targets can include target repo labels/paths."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Expands a matrix spec into local targets/report files and validates without execution.",
    ),
    CommandRemoteEffects(
        command="usertest matrix run",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_RUN_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Executes a validated matrix as local usertest batch runs; no built-in git/PR finalization.",
    ),
    CommandRemoteEffects(
        command="usertest lint",
        boundary="local-only",
        local_artifacts=Effect("may", "Writes a local lint JSON report when --out-json is used."),
        sensitive_artifacts=Effect("may", "Lint output can include local target/catalog paths."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Lints local runner/target configuration; git URL inputs may be acquired temporarily.",
    ),
    CommandRemoteEffects(
        command="usertest reports compile",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Compiles local run history JSONL from run directories.",
    ),
    CommandRemoteEffects(
        command="usertest reports analyze",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Analyzes a local history file and writes a local summary.",
    ),
    CommandRemoteEffects(
        command="usertest token-monitor analyze",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Writes metadata-only token monitoring artifacts for one local run directory.",
        modifiers=(RemoteEffectModifier("--no-write", "Prints analysis JSON without writing artifacts."),),
    ),
    CommandRemoteEffects(
        command="usertest token-monitor batch-context",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Writes metadata-only token monitoring artifacts for one local batch directory.",
    ),
    CommandRemoteEffects(
        command="usertest init-usertest",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Scaffolds a local target .usertest/ folder; does not publish it.",
    ),
    CommandRemoteEffects(
        command="usertest personas list",
        boundary="local-only",
        local_artifacts=Effect("may", "Git URL inputs may be cloned to a temporary directory for discovery."),
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Lists catalog entries; local paths are read in place, git URLs may be cloned temporarily.",
    ),
    CommandRemoteEffects(
        command="usertest missions list",
        boundary="local-only",
        local_artifacts=Effect("may", "Git URL inputs may be cloned to a temporary directory for discovery."),
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Lists catalog entries; local paths are read in place, git URLs may be cloned temporarily.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports compile",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Compiles local run directories into a local report history.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports analyze",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Analyzes local run history into local report artifacts.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports window",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Builds a local window summary from local run history.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports intent-snapshot",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=Effect("may", "Intent snapshots can include local repository metadata or prose."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Writes a local intent snapshot used by backlog analysis.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports review-ux",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Writes UX-review artifacts; the reviewer pass can be skipped with --dry-run.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Writes prompt/review artifacts but does not call an agent."),
        ),
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports sync-atom-actions",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Reconciles the local atom action ledger from local plan folders.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Reports reconciliation without writing the ledger."),
        ),
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports backlog",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Runs the backlog pipeline and writes local stage artifacts/backlog documents.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Synthesizes deterministic stage outputs without agent calls."),
        ),
    ),
    CommandRemoteEffects(
        command="usertest-backlog reports export-tickets",
        boundary="draft/export",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=Effect("default", "Exports local ticket templates and updates local action ledgers."),
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Exports staged backlog items as local draft ticket artifacts; does not push or create tracker issues.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog triage-prs",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=Effect("may", "Output reflects the supplied PR input JSON."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Clusters supplied PR JSON into local triage artifacts.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog triage-backlog",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=Effect("may", "Output reflects the supplied backlog input JSON."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Clusters supplied backlog JSON into local triage artifacts.",
    ),
    CommandRemoteEffects(
        command="usertest-backlog triage-atoms",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Clusters local backlog atoms and writes local triage artifacts.",
    ),
    CommandRemoteEffects(
        command="usertest-implement run",
        boundary="remote-write",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_RUN_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=Effect("default", "Default profile in configs/usertest_implement_settings.yaml sets commit: true."),
        pushes=Effect("default", "Default profile in configs/usertest_implement_settings.yaml sets push: true."),
        pull_requests=Effect("default", "Default profile in configs/usertest_implement_settings.yaml sets pr: true."),
        summary="Implements one ticket and, with the auto-loaded default settings, can commit, push, and create a PR.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Prints selected ticket, effective settings, and run request; does not run the agent or finalize git."),
            RemoteEffectModifier("--no-commit", "Disables commit; --push/--pr require a performed commit."),
            RemoteEffectModifier("--no-push", "Disables branch push."),
            RemoteEffectModifier("--no-pr", "Disables PR creation."),
            RemoteEffectModifier("--settings/--settings-profile", "Changes the effective defaults applied before execution."),
        ),
        defaults_source="configs/usertest_implement_settings.yaml profiles.default.run_common",
    ),
    CommandRemoteEffects(
        command="usertest-implement tickets run-next",
        boundary="remote-write",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_RUN_ARTIFACTS,
        draft_exports=Effect("default", "Refreshes local backlog exports unless --no-refresh-backlog is used."),
        commits=Effect("default", "Parser defaults and default settings set commit: true."),
        pushes=Effect("default", "Parser defaults and default settings set push: true."),
        pull_requests=Effect("default", "Parser defaults and default settings set pr: true."),
        summary="Refreshes backlog exports, selects the next local ticket, then implements it with commit/push/PR enabled by default.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Prints selected ticket, effective settings, and run request; does not run the agent or finalize git."),
            RemoteEffectModifier("--no-refresh-backlog", "Skips the backlog refresh/export phase."),
            RemoteEffectModifier("--no-commit/--no-push/--no-pr", "Disable the corresponding remote-write handoff steps."),
        ),
        defaults_source="configs/usertest_implement_settings.yaml profiles.default.run_common + tickets_run_next",
    ),
    CommandRemoteEffects(
        command="usertest-implement review run",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_RUN_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Runs a local implementation-review agent pass for an existing PR-backed ticket.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Prints the review run request without running the agent."),
        ),
    ),
    CommandRemoteEffects(
        command="usertest-implement review status",
        boundary="local-only",
        local_artifacts=NO_EFFECT,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Prints local review summary state from the ledger.",
    ),
    CommandRemoteEffects(
        command="usertest-implement review merge",
        boundary="remote-write",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=Effect("may", "Uses gh pr merge for an existing PR after review and CI gates are green."),
        summary="Merges an existing GitHub PR and moves the local ticket when the current gate is green.",
    ),
    CommandRemoteEffects(
        command="usertest-implement reports summarize",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_DERIVED_ARTIFACTS,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Summarizes local implementation runs into JSONL.",
    ),
    CommandRemoteEffects(
        command="usertest-implement tickets list",
        boundary="local-only",
        local_artifacts=NO_EFFECT,
        sensitive_artifacts=Effect("may", "Prints local ticket metadata and paths."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Prints the local ticket queue index.",
    ),
    CommandRemoteEffects(
        command="usertest-implement tickets next",
        boundary="local-only",
        local_artifacts=NO_EFFECT,
        sensitive_artifacts=Effect("may", "Prints local ticket metadata and paths."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Prints the next selected local ticket without moving it.",
    ),
    CommandRemoteEffects(
        command="usertest-implement tickets move",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Moves a local ticket file between local plan buckets and updates local ledgers.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Prints the destination without moving the ticket or updating ledgers."),
        ),
    ),
    CommandRemoteEffects(
        command="usertest-implement tickets discard",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Moves a local ticket to the discarded bucket and updates local action ledgers.",
    ),
    CommandRemoteEffects(
        command="usertest-implement batch run",
        boundary="remote-write",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=SENSITIVE_RUN_ARTIFACTS,
        draft_exports=Effect("may", "Batch refresh sources can run local backlog export steps."),
        commits=Effect("may", "Worker commands can inherit implementation settings that commit."),
        pushes=Effect("may", "Worker commands can inherit implementation settings that push."),
        pull_requests=Effect("may", "Worker commands can inherit implementation settings that create PRs."),
        summary="Runs a configured maintenance implementation batch; worker settings determine commit/push/PR behavior.",
    ),
    CommandRemoteEffects(
        command="usertest-implement batch status",
        boundary="local-only",
        local_artifacts=NO_EFFECT,
        sensitive_artifacts=Effect("may", "Prints local batch state."),
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Prints local batch state JSON.",
    ),
    CommandRemoteEffects(
        command="usertest-implement batch recover",
        boundary="local-only",
        local_artifacts=LOCAL_ARTIFACTS,
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Moves stale in-progress local tickets back to recoverable local buckets.",
    ),
    CommandRemoteEffects(
        command="usertest-implement maintenance-images list",
        boundary="local-only",
        local_artifacts=NO_EFFECT,
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Prints local Docker maintenance-image inventory.",
    ),
    CommandRemoteEffects(
        command="usertest-implement maintenance-images cleanup",
        boundary="local-only",
        local_artifacts=Effect("may", "Deletes local Docker image tags when --apply or config requests it."),
        sensitive_artifacts=NO_EFFECT,
        draft_exports=NO_EFFECT,
        commits=NO_EFFECT,
        pushes=NO_EFFECT,
        pull_requests=NO_EFFECT,
        summary="Prunes local Docker maintenance-image tags only; does not publish images.",
        modifiers=(
            RemoteEffectModifier("--dry-run", "Shows selected local image tags without deleting them."),
            RemoteEffectModifier("--apply", "Deletes selected local image tags."),
        ),
    ),
)

REMOTE_EFFECTS_BY_COMMAND: dict[str, CommandRemoteEffects] = {
    effect.command: effect for effect in REMOTE_EFFECTS
}

FIRST_USE_REMOTE_EFFECT_COMMANDS: tuple[str, ...] = (
    "usertest run",
    "usertest batch",
    "usertest report",
    "usertest init-usertest",
    "usertest-backlog reports backlog",
    "usertest-backlog reports export-tickets",
    "usertest-implement run",
    "usertest-implement tickets run-next",
    "usertest-implement tickets move",
    "usertest-implement review merge",
)


def get_remote_effect(command: str) -> CommandRemoteEffects:
    """Return the remote-effect classification for ``command``."""

    return REMOTE_EFFECTS_BY_COMMAND[command]


def first_use_remote_effects() -> tuple[CommandRemoteEffects, ...]:
    """Return command boundaries rendered in first-use documentation."""

    return tuple(REMOTE_EFFECTS_BY_COMMAND[command] for command in FIRST_USE_REMOTE_EFFECT_COMMANDS)


def _format_effect(effect: Effect) -> str:
    if effect.state == "never":
        return "No"
    if effect.state == "may":
        return "May"
    return "Default"


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def render_remote_effects_markdown_table(
    effects: tuple[CommandRemoteEffects, ...] | None = None,
) -> str:
    """Render a stable Markdown boundary table from the contract."""

    selected = effects if effects is not None else first_use_remote_effects()
    headers = [
        "Command",
        "Boundary",
        "Local artifacts",
        "Sensitive artifacts",
        "Draft/export",
        "Commit",
        "Push",
        "PR/merge",
        "Key modifiers / defaults",
    ]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for effect in selected:
        modifiers = [modifier.name for modifier in effect.modifiers]
        if effect.defaults_source:
            modifiers.append(f"defaults: {effect.defaults_source}")
        modifier_text = "; ".join(modifiers) if modifiers else "-"
        cells = [
            f"`{effect.command}`",
            effect.boundary,
            _format_effect(effect.local_artifacts),
            _format_effect(effect.sensitive_artifacts),
            _format_effect(effect.draft_exports),
            _format_effect(effect.commits),
            _format_effect(effect.pushes),
            _format_effect(effect.pull_requests),
            modifier_text,
        ]
        lines.append("| " + " | ".join(_escape_cell(cell) for cell in cells) + " |")
    return "\n".join(lines)


def validate_remote_effects_contract(
    effects: tuple[CommandRemoteEffects, ...] = REMOTE_EFFECTS,
) -> list[str]:
    """Return contract consistency errors without touching the network."""

    errors: list[str] = []
    seen: set[str] = set()
    for effect in effects:
        if effect.command in seen:
            errors.append(f"Duplicate remote-effects command: {effect.command}")
        seen.add(effect.command)
        if effect.boundary == "local-only" and (
            effect.commits.possible or effect.pushes.possible or effect.pull_requests.possible
        ):
            errors.append(f"{effect.command}: local-only boundary cannot include remote writes")
        if effect.boundary == "remote-write" and not (
            effect.commits.possible or effect.pushes.possible or effect.pull_requests.possible
        ):
            errors.append(f"{effect.command}: remote-write boundary must include a git/PR effect")
        if effect.draft_exports.possible and effect.boundary == "local-only":
            errors.append(f"{effect.command}: draft exports require draft/export or remote-write boundary")
    missing_first_use = set(FIRST_USE_REMOTE_EFFECT_COMMANDS) - seen
    if missing_first_use:
        errors.append(f"First-use docs command(s) missing from contract: {sorted(missing_first_use)!r}")
    return errors


def load_implement_settings_defaults(settings_path: Path) -> dict[str, Any]:
    """Load effective default ``run_common`` + ``tickets_run_next`` settings for validation."""

    doc = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Expected mapping in {settings_path}")
    profile_name = str(doc.get("default_profile") or "default")
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or profile_name not in profiles:
        raise ValueError(f"Default profile {profile_name!r} not found in {settings_path}")
    profile = profiles[profile_name]
    if not isinstance(profile, dict):
        raise ValueError(f"Default profile {profile_name!r} must be a mapping")

    run_common = profile.get("run_common", {})
    tickets_run_next = profile.get("tickets_run_next", {})
    if not isinstance(run_common, dict) or not isinstance(tickets_run_next, dict):
        raise ValueError("run_common and tickets_run_next settings must be mappings")
    merged = dict(run_common)
    merged.update(tickets_run_next)
    return merged
