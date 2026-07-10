"""Pipeline prompt-manifest loading and generic stage-prompt execution.

This module owns:
- ``PipelinePromptManifest``: the version-2 manifest dataclass.
- ``load_pipeline_prompt_manifest``: loads and validates the manifest from disk.
- ``run_stage_prompt_json``: generic helper that runs a single stage prompt through
  the agent backend and returns the raw text response.

All file references in the manifest are validated at load time.  Missing files raise
``FileNotFoundError`` loudly so no silent fallback to embedded defaults occurs.

Stage-specific orchestration lives in the CLI (``_run_problem_mining_stage``, etc.)
and calls this module's generic helpers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manifest dataclass
# ---------------------------------------------------------------------------

_MANIFEST_VERSION = 2

# Stage template key names (used for validation and lookup).
_STAGE_TEMPLATE_KEYS: tuple[str, ...] = (
    "problem_miner_templates",
    "relation_reviewer_template",
    "prioritizer_template",
    "solution_optioner_template",
    "solution_selector_template",
    "solution_falsifier_template",
    "change_planner_template",
    "selected_solution_labeler_template",
    "ux_reviewer_template",
)


@dataclass(frozen=True)
class PipelinePromptManifest:
    """Validated pipeline prompt manifest (version 2).

    All path attributes are absolute ``Path`` objects resolved relative to the
    prompts directory.  Missing files are caught at construction time.

    Attributes
    ----------
    prompts_dir:
        Directory from which this manifest was loaded.
    problem_miner_templates:
        Ordered list of problem-miner prompt template paths (one per pass type:
        default, onboarding, harness, schema).
    relation_reviewer_template:
        Generic relation-reviewer prompt path.
    prioritizer_template:
        Stage-2 prioritizer prompt path.
    solution_optioner_template:
        Stage-4 solution-optioner prompt path.
    solution_selector_template:
        Stage-5 solution-selector prompt path.
    solution_falsifier_template:
        Independent stage-5 falsification-review prompt path.
    change_planner_template:
        Stage-6 change-planner prompt path.
    selected_solution_labeler_template:
        Post-selection labeler prompt path (replaces old early labeler).
    ux_reviewer_template:
        UX reviewer prompt path.
    stage_guidance_manifest_path:
        Path to ``configs/backlog_stage_guidance/manifest.json``.
    taxonomy_path:
        Path to ``configs/backlog_taxonomy.json``.
    relation_review_config_path:
        Path to ``configs/backlog_relation_review.yaml``.
    """

    prompts_dir: Path
    problem_miner_templates: tuple[Path, ...]
    relation_reviewer_template: Path | None
    prioritizer_template: Path | None
    solution_optioner_template: Path | None
    solution_selector_template: Path | None
    solution_falsifier_template: Path | None
    change_planner_template: Path | None
    selected_solution_labeler_template: Path | None
    ux_reviewer_template: Path | None
    stage_guidance_manifest_path: Path
    taxonomy_path: Path
    relation_review_config_path: Path

    def template_text(self, path: Path | None) -> str:
        """Return the text content of an optional template path.

        Parameters
        ----------
        path:
            Template path, or ``None`` if the template was not configured.

        Returns
        -------
        str
            Template text.

        Raises
        ------
        FileNotFoundError
            When *path* is not ``None`` but does not exist.
        ValueError
            When *path* is ``None``.
        """
        if path is None:
            raise ValueError("template_text: template path is None (not yet configured)")
        if not path.exists():
            raise FileNotFoundError(
                f"Missing pipeline prompt template: {path}"
            )
        return path.read_text(encoding="utf-8")

    def load_taxonomy(self) -> dict[str, Any]:
        """Load and return the taxonomy JSON.

        Returns
        -------
        dict[str, Any]
            Parsed taxonomy document.

        Raises
        ------
        FileNotFoundError
            When the taxonomy file is missing.
        """
        if not self.taxonomy_path.exists():
            raise FileNotFoundError(
                f"Missing taxonomy config: {self.taxonomy_path}"
            )
        return json.loads(self.taxonomy_path.read_text(encoding="utf-8"))

    def load_stage_guidance(self, stage: str) -> str:
        """Load stage guidance text for *stage*.

        Reads the per-stage guidance Markdown file referenced in the stage-guidance
        manifest.

        Parameters
        ----------
        stage:
            Stage identifier string (e.g. ``"problem_mining"``).

        Returns
        -------
        str
            Guidance text for the stage.

        Raises
        ------
        FileNotFoundError
            When the stage-guidance manifest or the referenced stage file is missing.
        KeyError
            When *stage* is not listed in the stage-guidance manifest.
        """
        if not self.stage_guidance_manifest_path.exists():
            raise FileNotFoundError(
                f"Missing stage-guidance manifest: {self.stage_guidance_manifest_path}"
            )
        manifest_doc = json.loads(
            self.stage_guidance_manifest_path.read_text(encoding="utf-8")
        )
        stages_map: dict[str, str] = manifest_doc.get("stages") or {}
        if stage not in stages_map:
            raise KeyError(
                f"Stage {stage!r} not found in stage-guidance manifest "
                f"({self.stage_guidance_manifest_path}); "
                f"available stages: {sorted(stages_map)}"
            )
        guidance_filename = stages_map[stage]
        guidance_path = self.stage_guidance_manifest_path.parent / guidance_filename
        if not guidance_path.exists():
            raise FileNotFoundError(
                f"Missing stage guidance file: {guidance_path}"
            )
        return guidance_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------


def _resolve_optional_template(
    prompts_dir: Path, raw_manifest: dict[str, Any], key: str
) -> Path | None:
    """Resolve an optional template path from the manifest dict.

    Parameters
    ----------
    prompts_dir:
        Prompts directory.
    raw_manifest:
        Parsed manifest JSON.
    key:
        Key in *raw_manifest* to look up.

    Returns
    -------
    Path | None
        Resolved path if the key is present and non-empty, else ``None``.

    Raises
    ------
    FileNotFoundError
        When the key is present and points to a file that does not exist.
    """
    value = raw_manifest.get(key)
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError(
            "load_pipeline_prompt_manifest: expected string "
            f"for {key!r}, got {type(value).__name__}"
        )
    path = prompts_dir / value
    if not path.exists():
        raise FileNotFoundError(
            f"Missing pipeline prompt template: {path} (key={key!r})"
        )
    return path


def load_pipeline_prompt_manifest(prompts_dir: Path) -> PipelinePromptManifest:
    """Load and validate the pipeline prompt manifest from *prompts_dir*.

    The manifest file must be named ``pipeline_manifest.json`` and must declare
    ``"version": 2``.  All referenced files are validated at load time.  Missing
    files raise ``FileNotFoundError``; there is no silent fallback.

    Parameters
    ----------
    prompts_dir:
        Directory containing ``pipeline_manifest.json`` and referenced templates.

    Returns
    -------
    PipelinePromptManifest
        Validated manifest.

    Raises
    ------
    FileNotFoundError
        When the manifest file or any referenced file is missing.
    ValueError
        When the manifest has the wrong version or invalid structure.
    """
    manifest_path = prompts_dir / "pipeline_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Missing pipeline prompt manifest: {manifest_path}\n"
            "Create configs/backlog_prompts/pipeline_manifest.json with version=2."
        )

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"load_pipeline_prompt_manifest: invalid JSON in {manifest_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"load_pipeline_prompt_manifest: expected object in {manifest_path}, "
            f"got {type(raw).__name__}"
        )

    version = raw.get("version")
    if version != _MANIFEST_VERSION:
        raise ValueError(
            f"load_pipeline_prompt_manifest: expected version={_MANIFEST_VERSION} "
            f"in {manifest_path}, got {version!r}"
        )

    # Problem-miner templates (list).
    raw_miner_templates = raw.get("problem_miner_templates") or []
    if not isinstance(raw_miner_templates, list):
        raise ValueError(
            f"load_pipeline_prompt_manifest: 'problem_miner_templates' must be a list "
            f"in {manifest_path}"
        )
    miner_paths: list[Path] = []
    for item in raw_miner_templates:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"load_pipeline_prompt_manifest: invalid item in 'problem_miner_templates': "
                f"{item!r} in {manifest_path}"
            )
        p = prompts_dir / item
        if not p.exists():
            raise FileNotFoundError(
                f"Missing pipeline prompt template: {p} (problem_miner_templates)"
            )
        miner_paths.append(p)

    if not miner_paths:
        raise ValueError(
            f"load_pipeline_prompt_manifest: 'problem_miner_templates' is empty "
            f"in {manifest_path}"
        )

    # Optional stage templates.
    relation_reviewer = _resolve_optional_template(prompts_dir, raw, "relation_reviewer_template")
    prioritizer = _resolve_optional_template(prompts_dir, raw, "prioritizer_template")
    solution_optioner = _resolve_optional_template(prompts_dir, raw, "solution_optioner_template")
    solution_selector = _resolve_optional_template(prompts_dir, raw, "solution_selector_template")
    solution_falsifier = _resolve_optional_template(
        prompts_dir, raw, "solution_falsifier_template"
    )
    change_planner = _resolve_optional_template(prompts_dir, raw, "change_planner_template")
    sel_labeler = _resolve_optional_template(prompts_dir, raw, "selected_solution_labeler_template")
    ux_reviewer = _resolve_optional_template(prompts_dir, raw, "ux_reviewer_template")

    # Required config references.
    # These use repo-relative paths defined in the manifest.
    repo_root = prompts_dir.parent.parent  # configs/ → repo root
    stage_guidance_manifest_rel = raw.get("stage_guidance_manifest")
    if stage_guidance_manifest_rel:
        stage_guidance_manifest_path = repo_root / stage_guidance_manifest_rel
    else:
        stage_guidance_manifest_path = (
            repo_root / "configs" / "backlog_stage_guidance" / "manifest.json"
        )
    if not stage_guidance_manifest_path.exists():
        raise FileNotFoundError(
            f"Missing stage-guidance manifest: {stage_guidance_manifest_path}"
        )

    taxonomy_rel = raw.get("taxonomy_file")
    if taxonomy_rel:
        taxonomy_path = repo_root / taxonomy_rel
    else:
        taxonomy_path = repo_root / "configs" / "backlog_taxonomy.json"
    if not taxonomy_path.exists():
        raise FileNotFoundError(
            f"Missing taxonomy config: {taxonomy_path}"
        )

    relation_config_rel = raw.get("relation_review_config")
    if relation_config_rel:
        relation_review_config_path = repo_root / relation_config_rel
    else:
        relation_review_config_path = (
            repo_root / "configs" / "backlog_relation_review.yaml"
        )
    if not relation_review_config_path.exists():
        raise FileNotFoundError(
            f"Missing relation-review config: {relation_review_config_path}"
        )

    _LOG.info(
        "load_pipeline_prompt_manifest: loaded version=%d from %s "
        "(miner_templates=%d, taxonomy=%s)",
        _MANIFEST_VERSION,
        manifest_path,
        len(miner_paths),
        taxonomy_path,
    )

    return PipelinePromptManifest(
        prompts_dir=prompts_dir,
        problem_miner_templates=tuple(miner_paths),
        relation_reviewer_template=relation_reviewer,
        prioritizer_template=prioritizer,
        solution_optioner_template=solution_optioner,
        solution_selector_template=solution_selector,
        solution_falsifier_template=solution_falsifier,
        change_planner_template=change_planner,
        selected_solution_labeler_template=sel_labeler,
        ux_reviewer_template=ux_reviewer,
        stage_guidance_manifest_path=stage_guidance_manifest_path,
        taxonomy_path=taxonomy_path,
        relation_review_config_path=relation_review_config_path,
    )


# ---------------------------------------------------------------------------
# Generic stage prompt runner
# ---------------------------------------------------------------------------


def run_stage_prompt_json(
    *,
    stage: str,
    prompt: str,
    out_dir: Path,
    tag: str,
    agent: str,
    model: str | None,
    cfg: Any,
    workspace_dir: Path | None = None,
    allowed_tools: list[str] | None = None,
    include_directories: list[str] | None = None,
) -> str:
    """Run a stage prompt through the agent backend and return the raw response text.

    This is a generic helper used by each stage helper in the CLI.  Stage-specific
    orchestration passes the fully-rendered prompt and receives the raw LLM response
    string.  The caller is responsible for parsing the response with the appropriate
    ``stage_contracts`` parser.

    The prompt and raw response are written to *out_dir* / *tag* for auditability.

    Parameters
    ----------
    stage:
        Stage identifier string (for logging and file naming).
    prompt:
        Fully-rendered prompt text.
    out_dir:
        Directory where prompt and response artifacts are written.
    tag:
        Short tag for file naming (e.g. ``"problem_mining_001"``).
    agent:
        Agent identifier (e.g. ``"claude"``, ``"codex"``, ``"gemini"``).
    model:
        Optional model override.
    cfg:
        ``RunnerConfig`` instance.
    workspace_dir:
        Optional workspace directory passed to the agent backend.  When ``None``,
        the default workspace is used.
    allowed_tools:
        Optional list of tool names to allow for this stage prompt (agent-specific).
        When ``None``, the agent backend uses its default tool configuration.
    include_directories:
        Optional list of directories that tools are allowed to access (agent-specific).
        When ``None``, the agent backend uses its default directory policy.

    Returns
    -------
    str
        Raw text response from the agent.

    Raises
    ------
    RuntimeError
        When the agent backend returns an empty response.
    """
    from backlog_miner.agent import run_backlog_prompt

    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{tag}.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    _LOG.info("run_stage_prompt_json: stage=%s tag=%s agent=%s", stage, tag, agent)

    response = run_backlog_prompt(
        prompt=prompt,
        agent=agent,
        model=model,
        cfg=cfg,
        out_dir=out_dir,
        tag=tag,
        workspace_dir=workspace_dir,
        allowed_tools=allowed_tools,
        include_directories=include_directories,
    )

    if not response or not response.strip():
        raise RuntimeError(
            f"run_stage_prompt_json: empty response from agent for stage={stage} tag={tag}"
        )

    response_path = out_dir / f"{tag}.response.txt"
    response_path.write_text(response, encoding="utf-8")
    _LOG.info(
        "run_stage_prompt_json: stage=%s tag=%s response_len=%d", stage, tag, len(response)
    )
    return response
