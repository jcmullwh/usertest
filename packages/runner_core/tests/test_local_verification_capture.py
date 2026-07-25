from __future__ import annotations

import sys
from pathlib import Path

import pytest

from runner_core import capture_local_verification


def test_capture_local_verification_writes_exact_command_and_stream_artifacts(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "packages" / "capture_example" / "src" / "capture_example"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 'workspace-source'\n", encoding="utf-8")
    test_path = tmp_path / "test_capture_target.py"
    test_path.write_text(
        "from capture_example import VALUE\n\n"
        "def test_workspace_source():\n"
        "    assert VALUE == 'workspace-source'\n",
        encoding="utf-8",
    )
    command = "python -m pytest -q test_capture_target.py"
    run_dir = tmp_path / "run"

    summary = capture_local_verification(
        run_dir=run_dir,
        cwd=tmp_path,
        commands=[command],
        timeout_seconds=30.0,
        python_executable=sys.executable,
    )

    assert summary["passed"] is True
    assert summary["commands_configured"] == [command]
    assert summary["commands"][0]["command"] == command
    assert summary["model_invoked"] is False
    assert summary["workspace_mirror_written"] is False
    capture_dir = run_dir / "verification" / "capture"
    stdout = (capture_dir / "cmd_01.stdout.txt").read_text(encoding="utf-8")
    assert "1 passed" in stdout
    assert (capture_dir / "cmd_01.stderr.txt").is_file()
    assert (run_dir / "verification.json").is_file()


def test_capture_local_verification_refuses_to_overwrite_receipt(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "verification.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="overwrite"):
        capture_local_verification(
            run_dir=run_dir,
            cwd=tmp_path,
            commands=["python --version"],
            timeout_seconds=30.0,
            python_executable=sys.executable,
        )
