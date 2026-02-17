from __future__ import annotations

from pathlib import Path

from backlog_miner.ensemble import MinerJob, PromptManifest, load_prompt_manifest

__all__ = ["MinerJob", "PromptManifest", "load_prompt_manifest", "load_manifest"]


def load_manifest(prompts_dir: Path) -> PromptManifest:
    """Load backlog miner prompt manifest from disk.

    Parameters
    ----------
    prompts_dir:
        Directory containing ``manifest.json`` and referenced templates.

    Returns
    -------
    PromptManifest
        Validated prompt manifest payload.
    """

    return load_prompt_manifest(prompts_dir)
