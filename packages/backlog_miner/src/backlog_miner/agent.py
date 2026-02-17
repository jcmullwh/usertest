from __future__ import annotations

from pathlib import Path

from runner_core import RunnerConfig

from backlog_miner.ensemble import run_backlog_prompt as _run_backlog_prompt

__all__ = ["run_backlog_prompt"]


def run_backlog_prompt(
    *,
    agent: str,
    prompt: str,
    out_dir: Path,
    tag: str,
    model: str | None,
    cfg: RunnerConfig,
) -> str:
    """Run a single backlog prompt through the configured agent adapter.

    Parameters
    ----------
    agent:
        Agent identifier (for example ``"codex"``, ``"claude"``, ``"gemini"``).
    prompt:
        Fully rendered prompt text.
    out_dir:
        Artifact output directory for raw events and transcripts.
    tag:
        Stable run tag for artifact filenames.
    model:
        Optional model override.
    cfg:
        Runner configuration used to resolve agent binaries and policy.

    Returns
    -------
    str
        Final assistant text emitted by the selected agent run.
    """

    return _run_backlog_prompt(
        agent=agent,
        prompt=prompt,
        out_dir=out_dir,
        tag=tag,
        model=model,
        cfg=cfg,
    )
