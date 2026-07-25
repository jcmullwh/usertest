from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import runner_core.python_runtime as runtime_mod
import runner_core.runner as runner_mod


class TestPythonToolchainRegressionCoverage:
    """
    Offline integration-style regression tests for the canonical Python toolchain contract.
    Covers failure modes cited in the maintenance backlog ticket.
    """

    def test_missing_stdlib_interpreter_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        An interpreter that reports a version but fails to import stdlib (encodings)
        must be rejected with 'missing_stdlib'.
        """
        fake_python = tmp_path / "broken_python.exe"
        fake_python.write_bytes(b"")

        def _mock_run(
            args: list[str],
            **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            # Simulate missing stdlib error in stderr
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr=(
                "Fatal Python error: init_fs_encoding: failed to get the Python codec "
                "of the filesystem encoding\nModuleNotFoundError: No module named 'encodings'"
            ),
            )

        monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
        
        candidate = runtime_mod._probe_python_executable(
            str(fake_python),
            timeout_seconds=1.0,
            source="test",
        )

        assert candidate.usable is False
        assert candidate.reason_code == "missing_stdlib"
        assert "encodings" in candidate.reason

    def test_windowsapps_launcher_blocked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Paths matching WindowsApps Store aliases must be blocked immediately without probing.
        """
        store_path = (
            r"C:\Program Files\WindowsApps"
            r"\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe"
        )
        
        # We need to force windows platform for the check to trigger if running on Linux
        monkeypatch.setattr(runtime_mod, "_is_windows_platform", lambda **kwargs: True)

        candidate = runtime_mod._probe_python_executable(
            store_path,
            timeout_seconds=1.0,
            source="test",
            assume_present=True,
        )

        assert candidate.usable is False
        assert candidate.reason_code == "windowsapps_alias"
        assert "WindowsApps" in candidate.reason

    def test_pdm_blocked_when_python_toolchain_broken(self) -> None:
        """
        pdm is a Python wrapper; it must be marked unusable if the toolchain is broken.
        """
        command_diagnostics = {
            "pdm": {
                "present": True,
                "usable": True,
                "status": "present",
                "resolved_path": "/usr/bin/pdm",
            }
        }
        # Simulate a broken runtime (missing stdlib)
        python_runtime_summary = {
            "selected": {"path": "/usr/bin/python3", "usable": True},
            "candidates": [],
            "rejected": [],
        }
        context_probe = {
            "passed": False,
            "reason_code": "missing_stdlib",
            "reason": "ModuleNotFoundError: No module named 'encodings'",
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

    def test_absent_venv_falls_back_correctly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        When .venv is absent, select_python_runtime should skip it and move to next candidates.
        """
        workspace_dir = tmp_path / "empty_workspace"
        workspace_dir.mkdir()

        # Mock fallback python
        fallback_python = tmp_path / "sys_python.exe"
        fallback_python.write_bytes(b"")
        payload = json.dumps({"executable": str(fallback_python), "version": "3.13.2"})

        def _mock_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == str(fallback_python):
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=payload + "\n", stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

        monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
        monkeypatch.setattr(
            runtime_mod.shutil,
            "which",
            lambda cmd, **kwargs: str(fallback_python) if cmd == "python" else None,
        )
        monkeypatch.delenv("USERTEST_PYTHON", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        selection = runtime_mod.select_python_runtime(workspace_dir=workspace_dir)

        # workspace_venv should be present in candidates but not usable/present
        workspace_venv_candidate = next(
            c for c in selection.candidates if c.source == "workspace_venv"
        )
        assert workspace_venv_candidate.present is False
        assert workspace_venv_candidate.usable is False

        # Should have selected the fallback
        assert selection.selected is not None
        assert selection.selected.path == str(fallback_python)

    def test_host_metadata_mismatch_recovery(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        If USERTEST_PYTHON (from host metadata) is unusable in the current shell,
        the resolver must skip it and find a usable alternative.
        """
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        bad_host_python = "/host/path/python.exe"
        good_local_python = tmp_path / "local_python.exe"
        good_local_python.write_bytes(b"")
        
        payload = json.dumps({"executable": str(good_local_python), "version": "3.13.2"})

        def _mock_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == str(good_local_python):
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout=payload + "\n", stderr=""
                )
            # bad_host_python fails (not found or access denied)
            raise OSError("No such file or directory")

        monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
        monkeypatch.setattr(
            runtime_mod.shutil, 
            "which", 
            lambda cmd, **kwargs: str(good_local_python) if cmd == "python" else None
        )
        monkeypatch.setenv("USERTEST_PYTHON", bad_host_python)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        selection = runtime_mod.select_python_runtime(workspace_dir=workspace_dir)

        # sandbox_env candidate should be unusable
        sandbox_candidate = next(c for c in selection.candidates if c.source == "sandbox_env")
        assert sandbox_candidate.usable is False
        
        # Should have fell back to local python
        assert selection.selected is not None
        assert selection.selected.path == str(good_local_python)

    def test_preflight_and_execution_share_decision_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Confirms that the resolver returns a consistent decision that can be reused.
        """
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        
        python_exe = tmp_path / "python.exe"
        python_exe.write_bytes(b"")
        payload = json.dumps({"executable": str(python_exe), "version": "3.13.2"})

        def _mock_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=payload + "\n", stderr=""
            )

        monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
        monkeypatch.setattr(
            runtime_mod.shutil, 
            "which", 
            lambda cmd, **kwargs: str(python_exe)
        )

        # 1. Resolve during "preflight"
        selection_preflight = runtime_mod.select_python_runtime(workspace_dir=workspace_dir)
        assert selection_preflight.selected is not None
        resolved_path = selection_preflight.selected.path

        # 2. Simulate "execution" by using the resolved path
        # In the real runner, the resolved path is passed to command helpers
        # via the toolchain contract.
        
        # Verification command rewrite should use the SAME resolved path
        cmd, rewritten = runner_mod._rewrite_verification_command_for_python(
            "python -m pytest",
            python_executable=resolved_path,
            is_powershell=False,
        )
        assert rewritten is True
        assert resolved_path in cmd
        
        # Command probes should also be consistent if given the same path
        probe_result = runtime_mod.probe_python_interpreters(
            candidate_commands=[resolved_path]
        )
        assert probe_result.selected_resolved_path == resolved_path
        assert probe_result.selected_executable == str(python_exe)

    def test_validate_python_capability_orchestrates_canonical_decision(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Verify that _validate_python_capability (the canonical resolver contract)
        correctly combines selection and context probing.
        """
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        cwd = workspace_dir

        python_exe = tmp_path / "python.exe"
        python_exe.write_bytes(b"")
        
        # 1. Mock selection to return our python
        payload = json.dumps({"executable": str(python_exe), "version": "3.13.2"})
        def _mock_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=payload + "\n", stderr=""
            )
        monkeypatch.setattr(runtime_mod.subprocess, "run", _mock_run)
        monkeypatch.setattr(
            runtime_mod.shutil, 
            "which", 
            lambda cmd, **kwargs: str(python_exe)
        )
        monkeypatch.delenv("USERTEST_PYTHON", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        # 2. Mock the context probe (internal to runner.py)
        # _probe_python_context_capability is what performs the final validation
        monkeypatch.setattr(
            runner_mod,
            "_probe_python_context_capability",
            lambda **kwargs: {
                "passed": True, 
                "reason_code": None, 
                "metadata": {"version": "3.13.2", "executable": str(python_exe)}
            }
        )

        capability = runner_mod._validate_python_capability(
            workspace_dir=workspace_dir,
            verification_commands=("python -m pytest",),
            command_prefix=[],
            cwd=cwd,
            env_overrides=None,
        )

        assert capability["validation"]["required"] is True
        assert capability["validation"]["enabled"] is True
        assert capability["validation"]["validated_python_executable"] == str(python_exe)
        assert capability["runtime_summary"]["selected"]["path"] == str(python_exe)
        assert capability["context_probe"]["passed"] is True
