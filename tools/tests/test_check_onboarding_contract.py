from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_module(name: str, relative_path: str) -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_validate_repo_passes_for_current_onboarding_surfaces() -> None:
    module = _load_module("check_onboarding_contract", "check_onboarding_contract.py")
    repo_root = Path(__file__).resolve().parents[2]
    assert module.validate_repo(repo_root) == []


def test_validate_text_reports_missing_required_snippet() -> None:
    module = _load_module("check_onboarding_contract", "check_onboarding_contract.py")
    requirement = module.SurfaceRequirement(snippets=("alpha", "beta"), ordered_snippets=("alpha", "beta"))
    issues = module.validate_text(path=Path("README.md"), text="alpha only", requirement=requirement)
    assert any("missing required snippet: beta" in issue for issue in issues)
