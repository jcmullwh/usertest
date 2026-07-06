from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "runner_core.artifacts",
        "runner_core.git_helpers",
        "runner_core.preflight",
        "runner_core.prompt_staging",
        "runner_core.python_capability",
        "runner_core.shell_capability",
        "runner_core.stderr_diagnostics",
        "runner_core.verification_prompts",
    ],
)
def test_planned_runner_core_module_boundaries_import(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name
