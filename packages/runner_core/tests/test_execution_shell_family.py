from __future__ import annotations

from runner_core.runner import _execution_shell_family


def test_execution_shell_family_local_windows_is_powershell() -> None:
    assert _execution_shell_family(exec_backend="local", host_os="Windows") == "powershell"
    assert _execution_shell_family(exec_backend="local", host_os="windows") == "powershell"


def test_execution_shell_family_remote_backends_use_bash() -> None:
    assert _execution_shell_family(exec_backend="docker", host_os="Windows") == "bash"
    assert _execution_shell_family(exec_backend="docker", host_os="Linux") == "bash"


def test_execution_shell_family_local_non_windows_is_bash() -> None:
    assert _execution_shell_family(exec_backend="local", host_os="Linux") == "bash"
