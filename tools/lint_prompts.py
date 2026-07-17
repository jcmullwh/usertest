from __future__ import annotations

import json
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

# Banned untracked optimization adjectives.  These must not appear in stage prompt bodies
# unless they are injected via taxonomy text (i.e. their appearance is caused by template
# substitution, not by hard-coded prose).
_BANNED_OPTIMIZATION_TERMS: tuple[str, ...] = (
    "fastest",
    "quickest",
    "easiest",
    "simplest",
    "lowest-effort",
    "least-effort",
    "minimum-effort",
    "most convenient",
    "quick-win",
    "low-hanging",
)

# Stage-1 problem-mining prompt files (new pipeline).
_PROBLEM_MINER_PROMPTS: tuple[str, ...] = (
    "problem_miner_default.md",
    "problem_miner_onboarding.md",
    "problem_miner_harness.md",
    "problem_miner_schema.md",
)

# Fields that are forbidden in stage-1 output (no solution fields in problem records).
_PROBLEM_RECORD_FORBIDDEN_FIELDS: tuple[str, ...] = (
    "proposed_fix",
    "selected_solution",
    "family_id",
    "option_id",
    "implementation_steps",
)

# Stage-3 repro-research keywords that must be present.
_REPRO_RESEARCH_REQUIRED_TERMS: tuple[str, ...] = (
    "reproduction",
    "implementation_performed",
    "writes_purpose",
)

# Stage-3 anti-implementation terms that must be mentioned in the guardrail language.
_REPRO_RESEARCH_ANTI_IMPL_TERMS: tuple[str, ...] = (
    "not to implement",
    "not for implementation",
    "implementation is not",
    "goal is not to fix",
    "goal is not implementation",
)


def _read_text(path: Path) -> str:
    """Read a file's text, returning empty string if the file does not exist."""
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def lint_labeler_prompt_enums(*, repo_root: Path) -> list[str]:
    """Ensure the labeler prompt contains the exact surface-kind enum list.

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
    """Ensure miner prompts acknowledge the labeler stage (anti-command-sprawl guardrail).

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


def lint_stage_prompts_json_only(*, repo_root: Path) -> list[str]:
    """Ensure every stage prompt includes a JSON-only output rule.

    Stage prompts must contain a clear instruction to return JSON only.  This prevents
    free-text preamble from breaking downstream parsers.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    prompts_dir = repo_root / "configs" / "backlog_prompts"
    stage_prompt_globs = [
        "problem_miner_*.md",
        "relation_reviewer.md",
        "prioritizer.md",
        "solution_optioner.md",
        "solution_selector.md",
        "change_planner.md",
        "selected_solution_labeler.md",
    ]

    _JSON_ONLY_PHRASES = (
        "return only json",
        "return json only",
        "output must be",
        "json only",
        "output only json",
        "return a json",
        "return only a json",
        "return only valid json",
    )

    errors: list[str] = []
    for glob_pat in stage_prompt_globs:
        for path in sorted(prompts_dir.glob(glob_pat)):
            text = _read_text(path).lower()
            if not any(phrase in text for phrase in _JSON_ONLY_PHRASES):
                errors.append(f"stage_prompt_missing_json_only_rule: {path}")
    return errors


def lint_problem_miner_prompts_no_solution_fields(*, repo_root: Path) -> list[str]:
    """Ensure problem-miner prompts do not request solution fields.

    Problem-mining (stage 1) prompts must not ask for proposed_fix, selected_solution,
    or solution-family fields.  These fields are forbidden in problem records.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    prompts_dir = repo_root / "configs" / "backlog_prompts"
    errors: list[str] = []
    for name in _PROBLEM_MINER_PROMPTS:
        path = prompts_dir / name
        if not path.exists():
            # Not an error here; prompts are created incrementally.
            continue
        text = _read_text(path).lower()
        for field in _PROBLEM_RECORD_FORBIDDEN_FIELDS:
            if field in text:
                errors.append(
                    f"problem_miner_prompt_contains_forbidden_field: {path}: {field}"
                )
    return errors


def lint_problem_miner_origin_index_read_order(*, repo_root: Path) -> list[str]:
    """Require the Stage-1 miner to read both origin indexes in manifest order."""

    path = repo_root / "configs" / "backlog_prompts" / "problem_miner_default.md"
    if not path.exists():
        return [f"missing_prompt: {path}"]

    text = _read_text(path)
    fields = (
        "origin_attachment_evidence.manifest_file",
        "run_context.index_file",
        "assigned_evidence.index_file",
    )
    missing = [field for field in fields if field not in text]
    if missing:
        return [
            f"problem_miner_prompt_missing_origin_index_fields: {path}: "
            + ", ".join(missing)
        ]
    if not (text.index(fields[0]) < text.index(fields[1]) < text.index(fields[2])):
        return [f"problem_miner_prompt_origin_index_order_invalid: {path}"]
    return []


def lint_repro_research_prompt_anti_implementation(*, repo_root: Path) -> list[str]:
    """Ensure the repro-research prompt states that implementation is not the goal.

    The stage-3 repro-research prompt must explicitly say in multiple ways that the goal
    is reproduction and research, not implementation.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    path = repo_root / "configs" / "backlog_prompts" / "repro_researcher.md"
    if not path.exists():
        # Not yet created; skip until the prompt exists.
        return []
    text = _read_text(path).lower()

    errors: list[str] = []
    if "reproduction" not in text and "reproduce" not in text:
        errors.append(f"repro_research_prompt_missing_reproduction_goal: {path}")
    if "implementation_performed" not in text:
        errors.append(
            f"repro_research_prompt_missing_implementation_performed_field: {path}"
        )

    if not any(phrase in text for phrase in _REPRO_RESEARCH_ANTI_IMPL_TERMS):
        errors.append(
            f"repro_research_prompt_missing_anti_implementation_guardrail: {path}: "
            "prompt must say in multiple ways that implementation is not the goal"
        )
    return errors


def lint_solution_selector_prompt_no_option_invention(*, repo_root: Path) -> list[str]:
    """Ensure the solution-selector prompt restricts choices to supplied options.

    The selector prompt must not invite the model to create new options.  It should
    state that it must choose from the supplied option set.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    path = repo_root / "configs" / "backlog_prompts" / "solution_selector.md"
    if not path.exists():
        return []
    text = _read_text(path).lower()

    errors: list[str] = []
    choose_from_phrases = (
        "choose from",
        "select from",
        "from the supplied",
        "from the provided",
        "from the option",
        "do not create",
        "do not invent",
        "must not invent",
    )
    if not any(phrase in text for phrase in choose_from_phrases):
        errors.append(
            f"solution_selector_prompt_missing_choose_from_constraint: {path}: "
            "prompt must instruct the model to select from supplied options, not create new ones"
        )
    return errors


def lint_stage_prompts_no_banned_optimization_terms(*, repo_root: Path) -> list[str]:
    """Ensure stage prompts do not contain hard-coded banned optimization adjectives.

    Banned terms (fastest, simplest, etc.) are allowed only when they appear inside
    a template-injection placeholder (e.g., ``{{TAXONOMY_TEXT}}``) to indicate that
    the text comes from the taxonomy config, not from the prompt author.

    This check looks for banned terms in the prompt text after stripping all
    ``{{...}}`` injection blocks, so genuine taxonomy injections are excluded.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    import re

    prompts_dir = repo_root / "configs" / "backlog_prompts"
    stage_prompt_globs = [
        "problem_miner_*.md",
        "relation_reviewer.md",
        "prioritizer.md",
        "solution_optioner.md",
        "solution_selector.md",
        "change_planner.md",
        "selected_solution_labeler.md",
    ]

    # Strip injection blocks so taxonomy-injected text is not counted against the prompt.
    _INJECTION_RE = re.compile(r"\{\{[^}]+\}\}", re.DOTALL)

    errors: list[str] = []
    for glob_pat in stage_prompt_globs:
        for path in sorted(prompts_dir.glob(glob_pat)):
            raw = _read_text(path)
            # Remove injection placeholders before scanning.
            stripped = _INJECTION_RE.sub("", raw).lower()
            for term in _BANNED_OPTIMIZATION_TERMS:
                if term in stripped:
                    errors.append(
                        f"stage_prompt_banned_optimization_term: {path}: "
                        f"'{term}' must not appear in prompt body; "
                        "use solution-family labels from taxonomy instead"
                    )
    return errors


def lint_pipeline_manifest_references_exist(*, repo_root: Path) -> list[str]:
    """Ensure the pipeline prompt manifest (version 2) references existing files.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        Lint errors. Empty means success.
    """

    manifest_path = repo_root / "configs" / "backlog_prompts" / "pipeline_manifest.json"
    if not manifest_path.exists():
        # Not yet created; skip.
        return []

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"pipeline_manifest_invalid_json: {manifest_path}: {exc}"]

    if manifest.get("version") != 2:
        return [
            f"pipeline_manifest_wrong_version: {manifest_path}: "
            f"expected version=2, got {manifest.get('version')!r}"
        ]

    prompts_dir = repo_root / "configs" / "backlog_prompts"
    taxonomy_path = repo_root / "configs" / "backlog_taxonomy.json"
    relation_config_path = repo_root / "configs" / "backlog_relation_review.yaml"
    stage_guidance_dir = repo_root / "configs" / "backlog_stage_guidance"

    errors: list[str] = []

    # Check that referenced prompt templates exist.
    for key, value in manifest.get("stage_templates", {}).items():
        if isinstance(value, str):
            p = prompts_dir / value
            if not p.exists():
                errors.append(
                    f"pipeline_manifest_missing_stage_template: {manifest_path}: "
                    f"stage={key} file={value}"
                )
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    p = prompts_dir / item
                    if not p.exists():
                        errors.append(
                            f"pipeline_manifest_missing_stage_template: {manifest_path}: "
                            f"stage={key} file={item}"
                        )

    # Check that referenced taxonomy and config files exist.
    if not taxonomy_path.exists():
        errors.append(f"pipeline_manifest_missing_taxonomy: {taxonomy_path}")
    if not relation_config_path.exists():
        errors.append(
            f"pipeline_manifest_missing_relation_review_config: {relation_config_path}"
        )
    if not stage_guidance_dir.exists():
        errors.append(
            f"pipeline_manifest_missing_stage_guidance_dir: {stage_guidance_dir}"
        )

    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    Run from repo root:

        python tools/lint_prompts.py

    Returns
    -------
    int
        Exit code.  0 = success, 1 = lint failures.
    """

    _ = argv
    repo_root = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    errors.extend(lint_labeler_prompt_enums(repo_root=repo_root))
    errors.extend(lint_miner_prompt_mentions_labeler(repo_root=repo_root))
    errors.extend(lint_stage_prompts_json_only(repo_root=repo_root))
    errors.extend(lint_problem_miner_prompts_no_solution_fields(repo_root=repo_root))
    errors.extend(lint_problem_miner_origin_index_read_order(repo_root=repo_root))
    errors.extend(lint_repro_research_prompt_anti_implementation(repo_root=repo_root))
    errors.extend(lint_solution_selector_prompt_no_option_invention(repo_root=repo_root))
    errors.extend(lint_stage_prompts_no_banned_optimization_terms(repo_root=repo_root))
    errors.extend(lint_pipeline_manifest_references_exist(repo_root=repo_root))
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
