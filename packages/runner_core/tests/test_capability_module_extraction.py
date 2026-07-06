from __future__ import annotations

from types import SimpleNamespace

from runner_core.preflight import (
    _build_preflight_command_list,
    _format_windows_python_preflight_error,
)
from runner_core.python_capability import _build_python_toolchain_capability_summary
from runner_core.shell_capability import _resolve_shell_capability


def test_preflight_command_builder_is_available_from_focused_module() -> None:
    request = SimpleNamespace(
        preflight_commands=("custom-tool", "rg"),
        preflight_required_commands=("required-tool", "custom-tool"),
    )

    commands = _build_preflight_command_list(request)

    assert "git" in commands
    assert "custom-tool" in commands
    assert "required-tool" in commands
    assert commands.count("rg") == 1
    assert commands.count("custom-tool") == 1


def test_shell_capability_resolver_is_available_from_focused_module() -> None:
    capability = _resolve_shell_capability(
        agent="claude",
        operating_system="Linux",
        backend="local",
        sandbox_mode=None,
        policy_status="allowed",
        policy_reason="claude.allowed_tools includes Bash",
        allowed_tools=["Bash"],
        probe_result={"kind": "backend_shell_payload", "ok": True, "exit_code": 0},
    ).to_dict()

    assert capability["state"] == "available"
    assert capability["probe_status"] == "passed"
    assert capability["reason_code"] is None


def test_python_capability_summary_is_available_from_focused_module() -> None:
    summary = _build_python_toolchain_capability_summary(
        python_validation_required=True,
        python_validation_enabled=False,
        python_validation_reason_code="windowsapps_alias",
        python_validation_reason_type="discovery",
        python_validation_reason="Resolved to a WindowsApps launcher alias.",
        python_context_probe={"passed": False},
        validated_python_executable=None,
        pdm_required=True,
    )

    assert summary == {
        "toolchain_status": "blocked",
        "python_required": True,
        "pdm_required": True,
        "interpreter_usable": False,
        "context_probe_passed": False,
        "reason_code": "windowsapps_alias",
        "reason_type": "discovery",
        "reason": "Resolved to a WindowsApps launcher alias.",
        "validated_executable": None,
    }


def test_windows_python_preflight_remediation_formatting_stays_stable() -> None:
    probe = SimpleNamespace(
        to_dict=lambda: {
            "candidates": [
                {
                    "command": "python",
                    "resolved_path": (
                        r"C:\\Users\\me\\AppData\\Local\\Microsoft"
                        r"\\WindowsApps\\python.exe"
                    ),
                    "reason_code": "windowsapps_alias",
                    "reason": "Resolved to a WindowsApps launcher alias.",
                }
            ]
        }
    )

    message = _format_windows_python_preflight_error(probe)

    assert "Python preflight failed on Windows" in message
    assert "WindowsApps" in message
    assert "winget install -e --id Python.Python.3.13" in message
    assert "App execution aliases" in message
    assert "--exec-backend docker" in message
