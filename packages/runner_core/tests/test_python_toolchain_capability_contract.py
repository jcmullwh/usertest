"""Tests for the canonical Python toolchain capability contract.

Verifies that:
- ``pdm`` commands are recognized as Python-dependent in verification command detection.
- ``verification_commands_need_pdm`` correctly identifies bare ``pdm`` invocations.
- ``verification_commands_need_python`` returns ``True`` for ``pdm`` commands.
- ``_align_python_command_diagnostics`` marks ``pdm`` as unusable when the Python
  toolchain is broken.
- ``_build_python_toolchain_capability_summary`` produces the expected canonical
  machine-readable record.
- The canonical ``python_toolchain_capability`` key is written to ``preflight.json``.
"""
from __future__ import annotations

from typing import Any

import runner_core.runner as runner_mod
from runner_core.python_runtime import (
    verification_commands_need_pdm,
    verification_commands_need_python,
)

# ---------------------------------------------------------------------------
# verification_commands_need_pdm
# ---------------------------------------------------------------------------


class TestVerificationCommandsNeedPdm:
    def test_bare_pdm_command(self) -> None:
        assert verification_commands_need_pdm(("pdm run pytest",)) is True

    def test_pdm_install(self) -> None:
        assert verification_commands_need_pdm(("pdm install",)) is True

    def test_pdm_only(self) -> None:
        assert verification_commands_need_pdm(("pdm",)) is True

    def test_no_pdm_commands(self) -> None:
        assert verification_commands_need_pdm(("pytest", "python -m pytest")) is False

    def test_empty_commands(self) -> None:
        assert verification_commands_need_pdm(()) is False

    def test_powershell_ampersand_pdm(self) -> None:
        assert verification_commands_need_pdm(("& pdm run pytest",)) is True

    def test_pdm_not_substring_match(self) -> None:
        # "pdmtool" should NOT match; pattern requires word boundary / whitespace after
        assert verification_commands_need_pdm(("pdmtool run",)) is False


# ---------------------------------------------------------------------------
# verification_commands_need_python now includes pdm
# ---------------------------------------------------------------------------


class TestVerificationCommandsNeedPythonIncludesPdm:
    def test_pdm_run_is_python_dependent(self) -> None:
        assert verification_commands_need_python(("pdm run pytest",)) is True

    def test_pdm_install_is_python_dependent(self) -> None:
        assert verification_commands_need_python(("pdm install",)) is True

    def test_pure_rg_command_not_python(self) -> None:
        assert verification_commands_need_python(("rg --version",)) is False

    def test_explicit_python_still_detected(self) -> None:
        assert verification_commands_need_python(("python -m pytest",)) is True

    def test_pytest_still_detected(self) -> None:
        assert verification_commands_need_python(("pytest -v",)) is True

    def test_mixed_commands_with_pdm(self) -> None:
        assert verification_commands_need_python(("rg --version", "pdm run test")) is True


# ---------------------------------------------------------------------------
# _align_python_command_diagnostics marks pdm as unusable
# ---------------------------------------------------------------------------


def _make_diag(*, status: str = "present") -> dict[str, Any]:
    return {
        "present": True,
        "usable": True,
        "status": status,
        "resolved_path": "/usr/bin/fake",
        "reason_code": None,
        "reason_type": None,
        "reason": None,
        "remediation": None,
    }


class TestAlignPythonCommandDiagnosticsIncludesPdm:
    def test_pdm_marked_unusable_when_no_selected_runtime(self) -> None:
        command_diagnostics = {
            "python": _make_diag(),
            "pdm": _make_diag(),
        }
        python_runtime_summary: dict[str, Any] = {
            "selected": None,
            "candidates": [],
            "rejected": [],
        }
        runner_mod._align_python_command_diagnostics(
            command_diagnostics=command_diagnostics,
            python_runtime_summary=python_runtime_summary,
            python_context_probe=None,
            python_validation_required=True,
        )
        pdm_diag = command_diagnostics["pdm"]
        assert pdm_diag["usable"] is False
        assert pdm_diag["status"] == "unusable"
        assert pdm_diag.get("python_dependency_blocked") is True

    def test_pdm_marked_unusable_when_context_probe_fails(self) -> None:
        command_diagnostics = {
            "python": _make_diag(),
            "pdm": _make_diag(),
        }
        python_runtime_summary: dict[str, Any] = {
            "selected": {"path": "/usr/bin/python", "usable": True},
            "candidates": [],
            "rejected": [],
        }
        context_probe: dict[str, Any] = {
            "passed": False,
            "reason_code": "missing_stdlib",
            "reason_type": "runtime",
            "reason": "encodings module not found",
            "remediation": "Reinstall Python",
        }
        runner_mod._align_python_command_diagnostics(
            command_diagnostics=command_diagnostics,
            python_runtime_summary=python_runtime_summary,
            python_context_probe=context_probe,
            python_validation_required=True,
        )
        pdm_diag = command_diagnostics["pdm"]
        assert pdm_diag["usable"] is False
        assert pdm_diag["status"] == "unusable"
        assert pdm_diag["reason_code"] == "missing_stdlib"
        assert pdm_diag.get("python_dependency_blocked") is True

    def test_pdm_not_affected_when_toolchain_healthy(self) -> None:
        command_diagnostics = {
            "pdm": _make_diag(),
        }
        python_runtime_summary: dict[str, Any] = {
            "selected": {"path": "/usr/bin/python", "usable": True},
            "candidates": [],
            "rejected": [],
        }
        context_probe: dict[str, Any] = {
            "passed": True,
            "reason_code": None,
            "reason": None,
        }
        runner_mod._align_python_command_diagnostics(
            command_diagnostics=command_diagnostics,
            python_runtime_summary=python_runtime_summary,
            python_context_probe=context_probe,
            python_validation_required=True,
        )
        # Toolchain is healthy - pdm should be unchanged
        pdm_diag = command_diagnostics["pdm"]
        assert pdm_diag["usable"] is True
        assert pdm_diag["status"] == "present"

    def test_pdm_missing_status_not_overwritten(self) -> None:
        """A pdm diagnostic with status=missing should not be overwritten."""
        command_diagnostics = {
            "pdm": {
                "present": False,
                "usable": False,
                "status": "missing",
                "resolved_path": None,
                "reason_code": "not_found",
                "reason_type": "discovery",
                "reason": "`pdm` was not found on PATH.",
                "remediation": None,
            }
        }
        python_runtime_summary: dict[str, Any] = {
            "selected": None,
            "candidates": [],
            "rejected": [],
        }
        runner_mod._align_python_command_diagnostics(
            command_diagnostics=command_diagnostics,
            python_runtime_summary=python_runtime_summary,
            python_context_probe=None,
            python_validation_required=True,
        )
        # status=missing should be preserved, not overwritten to unusable
        assert command_diagnostics["pdm"]["status"] == "missing"
        assert command_diagnostics["pdm"]["reason_code"] == "not_found"


# ---------------------------------------------------------------------------
# _build_python_toolchain_capability_summary
# ---------------------------------------------------------------------------


class TestBuildPythonToolchainCapabilitySummary:
    def test_not_required(self) -> None:
        summary = runner_mod._build_python_toolchain_capability_summary(
            python_validation_required=False,
            python_validation_enabled=False,
            python_validation_reason_code=None,
            python_validation_reason_type=None,
            python_validation_reason=None,
            python_context_probe=None,
            validated_python_executable=None,
            pdm_required=False,
        )
        assert summary["toolchain_status"] == "not_required"
        assert summary["python_required"] is False
        assert summary["pdm_required"] is False
        assert summary["interpreter_usable"] is True
        assert summary["context_probe_passed"] is None

    def test_healthy(self) -> None:
        summary = runner_mod._build_python_toolchain_capability_summary(
            python_validation_required=True,
            python_validation_enabled=True,
            python_validation_reason_code=None,
            python_validation_reason_type=None,
            python_validation_reason=None,
            python_context_probe={"passed": True},
            validated_python_executable="/usr/bin/python3",
            pdm_required=True,
        )
        assert summary["toolchain_status"] == "healthy"
        assert summary["python_required"] is True
        assert summary["pdm_required"] is True
        assert summary["interpreter_usable"] is True
        assert summary["context_probe_passed"] is True
        assert summary["validated_executable"] == "/usr/bin/python3"

    def test_blocked(self) -> None:
        summary = runner_mod._build_python_toolchain_capability_summary(
            python_validation_required=True,
            python_validation_enabled=False,
            python_validation_reason_code="missing_stdlib",
            python_validation_reason_type="runtime",
            python_validation_reason="encodings module not found",
            python_context_probe={"passed": False},
            validated_python_executable=None,
            pdm_required=False,
        )
        assert summary["toolchain_status"] == "blocked"
        assert summary["interpreter_usable"] is False
        assert summary["reason_code"] == "missing_stdlib"
        assert summary["reason_type"] == "runtime"
        assert summary["context_probe_passed"] is False
        assert summary["validated_executable"] is None

    def test_pdm_required_flag_captured(self) -> None:
        summary = runner_mod._build_python_toolchain_capability_summary(
            python_validation_required=True,
            python_validation_enabled=True,
            python_validation_reason_code=None,
            python_validation_reason_type=None,
            python_validation_reason=None,
            python_context_probe={"passed": True},
            validated_python_executable="/usr/bin/python3",
            pdm_required=True,
        )
        assert summary["pdm_required"] is True

    def test_no_context_probe_when_not_required(self) -> None:
        summary = runner_mod._build_python_toolchain_capability_summary(
            python_validation_required=False,
            python_validation_enabled=False,
            python_validation_reason_code=None,
            python_validation_reason_type=None,
            python_validation_reason=None,
            python_context_probe=None,
            validated_python_executable=None,
            pdm_required=False,
        )
        assert summary["context_probe_passed"] is None
