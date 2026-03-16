from __future__ import annotations

import os
from pathlib import Path

from usertest.first_run_launcher import main


def _write_fake_python(path: Path) -> None:
    if os.name == "nt":
        body = """@echo off
setlocal EnableDelayedExpansion
>> "%FAKE_PYTHON_LOG%" echo %*	%PYTHONPATH%

if /I "%~1"=="tools/scaffold/scaffold.py" goto success
if /I "%~1"=="tools/smoke_import_guard.py" goto guard
if /I "%~1"=="-m" goto module
if /I "%~1"=="-c" goto preflight

>&2 echo unexpected args: %*
exit /b 2

:guard
if "%FAKE_GUARD_FAIL_ONCE%"=="1" if not exist "%FAKE_GUARD_MARKER%" (
  type nul > "%FAKE_GUARD_MARKER%"
  >&2 echo guard failed
  exit /b 1
)
echo guard ok
exit /b 0

:module
if /I "%~2"=="pip" if /I "%~3"=="--version" (
  echo pip 25.0 from fake
  exit /b 0
)
if /I "%~2"=="pip" if /I "%~3"=="install" goto success
if /I "%~2"=="usertest.cli" if /I "%~3"=="--help" goto success
if /I "%~2"=="usertest_backlog.cli" if /I "%~3"=="--help" goto success
if /I "%~2"=="usertest_implement.cli" if /I "%~3"=="--help" goto success
if /I "%~2"=="pytest" goto success
>&2 echo unexpected module args: %*
exit /b 2

:preflight
if "%FAKE_IMPORT_PREFLIGHT_FAIL%"=="1" (
  echo usertest: ModuleNotFoundError: No module named 'usertest'
  exit /b 1
)
exit /b 0

:success
exit /b 0
"""
        path = path.with_suffix(".cmd")
        path.write_text(body.replace("\n", "\r\n"), encoding="utf-8")
        return

    body = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\t%s\n' \"$*\" \"${PYTHONPATH-}\" >> \"${FAKE_PYTHON_LOG:?}\"

if [[ \"${1:-}\" == \"tools/scaffold/scaffold.py\" ]]; then
  exit 0
fi

if [[ \"${1:-}\" == \"tools/smoke_import_guard.py\" ]]; then
  if [[ \"${FAKE_GUARD_FAIL_ONCE:-0}\" == \"1\" && ! -f \"${FAKE_GUARD_MARKER:?}\" ]]; then
    : > \"${FAKE_GUARD_MARKER}\"
    printf 'guard failed\\n' >&2
    exit 1
  fi
  printf 'guard ok\\n'
  exit 0
fi

if [[ \"${1:-}\" == \"-m\" ]]; then
  if [[ \"${2:-}\" == \"pip\" && \"${3:-}\" == \"--version\" ]]; then
    printf 'pip 25.0 from fake\\n'
    exit 0
  fi
  if [[ \"${2:-}\" == \"pip\" && \"${3:-}\" == \"install\" ]]; then
    exit 0
  fi
  if [[ \"${2:-}\" == \"usertest.cli\" && \"${3:-}\" == \"--help\" ]]; then
    exit 0
  fi
  if [[ \"${2:-}\" == \"usertest_backlog.cli\" && \"${3:-}\" == \"--help\" ]]; then
    exit 0
  fi
  if [[ \"${2:-}\" == \"usertest_implement.cli\" && \"${3:-}\" == \"--help\" ]]; then
    exit 0
  fi
  if [[ \"${2:-}\" == \"pytest\" ]]; then
    exit 0
  fi
fi

if [[ \"${1:-}\" == \"-c\" ]]; then
  if [[ \"${2:-}\" == *\"import importlib\"* ]]; then
    if [[ \"${FAKE_IMPORT_PREFLIGHT_FAIL:-0}\" == \"1\" ]]; then
      printf \"usertest: ModuleNotFoundError: No module named 'usertest'\\n\"
      exit 1
    fi
    exit 0
  fi
fi

printf 'unexpected args: %s\\n' \"$*\" >&2
exit 2
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def test_shared_launcher_reports_shell_specific_skip_install_guidance(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_python = tmp_path / "fake-python"
    log_path = tmp_path / "calls.log"
    _write_fake_python(fake_python)
    fake_python_path = fake_python.with_suffix(".cmd") if os.name == "nt" else fake_python

    monkeypatch.setenv("FAKE_PYTHON_LOG", str(log_path))
    monkeypatch.setenv("FAKE_IMPORT_PREFLIGHT_FAIL", "1")

    rc = main(
        [
            "smoke",
            "--repo-root",
            str(repo_root),
            "--python",
            str(fake_python_path),
            "--python-source",
            "test",
            "--shell",
            "powershell",
            "--skip-install",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "Smoke preflight failed" in captured.err
    assert (
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        ".\\scripts\\smoke.ps1 -UsePythonPath"
    ) in captured.err
    assert "tools/smoke_import_guard.py" not in log_path.read_text(encoding="utf-8")


def test_shared_launcher_falls_back_to_pythonpath_after_guard_failure(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_python = tmp_path / "fake-python"
    log_path = tmp_path / "calls.log"
    marker_path = tmp_path / "guard.once"
    _write_fake_python(fake_python)
    fake_python_path = fake_python.with_suffix(".cmd") if os.name == "nt" else fake_python

    monkeypatch.setenv("FAKE_PYTHON_LOG", str(log_path))
    monkeypatch.setenv("FAKE_GUARD_FAIL_ONCE", "1")
    monkeypatch.setenv("FAKE_GUARD_MARKER", str(marker_path))

    rc = main(
        [
            "smoke",
            "--repo-root",
            str(repo_root),
            "--python",
            str(fake_python_path),
            "--python-source",
            "test",
            "--shell",
            "posix",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "switching to PYTHONPATH mode" in captured.out
    lines = [
        line.replace("\\", "/")
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        "tools/smoke_import_guard.py --repo-root" in line and "apps/usertest/src" in line
        for line in lines
    )
    assert any("-m usertest.cli --help" in line and "apps/usertest/src" in line for line in lines)
    assert any("-m pytest -q apps/usertest/tests/test_smoke.py" in line for line in lines)
