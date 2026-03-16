from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from usertest_implement.cli import _one_command_first_success_remediation


def _load_contract() -> ModuleType:
    module_path = Path(__file__).resolve().parents[3] / "tools" / "onboarding_contract.py"
    spec = importlib.util.spec_from_file_location("onboarding_contract", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["onboarding_contract"] = module
    spec.loader.exec_module(module)
    return module


def test_implement_cli_uses_canonical_quick_fix_text() -> None:
    contract = _load_contract()
    assert _one_command_first_success_remediation() == (
        contract.one_command_first_success_remediation()
    )
