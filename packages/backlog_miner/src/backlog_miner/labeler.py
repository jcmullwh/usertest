from __future__ import annotations

from pathlib import Path
from typing import Any

from runner_core import RunnerConfig

from backlog_miner.ensemble import (
    PromptManifest,
)
from backlog_miner.ensemble import (
    run_labeler_jobs as _run_labeler_jobs,
)

__all__ = ["run_labeler_jobs"]


def run_labeler_jobs(
    *,
    tickets: list[dict[str, Any]],
    atoms_by_id: dict[str, dict[str, Any]],
    prompts_dir: Path,
    prompt_manifest: PromptManifest,
    artifacts_dir: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    labelers: int,
    resume: bool,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Run labeler ensemble stage and return patched ticket payload.

    Parameters
    ----------
    tickets:
        Ticket list emitted by miner/merge stages.
    atoms_by_id:
        Atom lookup map used for evidence expansion.
    prompts_dir:
        Directory containing prompt templates and manifest.
    prompt_manifest:
        Parsed prompt manifest.
    artifacts_dir:
        Root artifact directory for labeler outputs.
    agent:
        Agent identifier used to execute prompts.
    model:
        Optional model override.
    cfg:
        Runner configuration.
    labelers:
        Number of labeler variants to execute per ticket.
    resume:
        Reuse cached labeler artifacts when available.
    force:
        Ignore cached artifacts and rerun labelers.
    dry_run:
        Write prompts without invoking agent binaries.

    Returns
    -------
    dict[str, Any]
        Updated tickets plus labeler metadata.
    """

    return _run_labeler_jobs(
        tickets=tickets,
        atoms_by_id=atoms_by_id,
        prompts_dir=prompts_dir,
        prompt_manifest=prompt_manifest,
        artifacts_dir=artifacts_dir,
        agent=agent,
        model=model,
        cfg=cfg,
        labelers=labelers,
        resume=resume,
        force=force,
        dry_run=dry_run,
    )
