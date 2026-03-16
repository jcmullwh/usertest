from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _tool_path() -> Path:
    return Path(__file__).resolve().parents[3] / "tools" / "python_toolchain.py"


def _run_tool(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_tool_path()), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _real_python_executable() -> str:
    override = os.environ.get("USERTEST_TEST_PYTHON")
    if override:
        return override
    if os.name == "nt":
        proc = subprocess.run(
            ["py", "-0p"],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                text = line.strip()
                if not text or " " not in text:
                    continue
                candidate = text.rsplit(" ", 1)[-1].strip()
                if Path(candidate).exists():
                    return candidate
    return sys.executable


def _write_fake_python(path: Path, *, pdm_missing: bool = False, venv_fails: bool = False) -> None:
    if os.name == "nt":
        real_python = _real_python_executable()
        python_probe = (
            f'"{real_python}" -c "import json; '
            "print(json.dumps({"
            r"'executable': r'%~f0', 'version': '3.13.2', 'prefix': r'C:\\fake', "
            r"'base_prefix': r'C:\\fake', 'real_prefix': '', 'exec_prefix': r'C:\\fake', "
            r"'base_exec_prefix': r'C:\\fake', 'virtual_env': ''}))"
            '"'
        )
        body = [
            "@echo off",
            "setlocal",
            'if "%~1"=="-c" (',
            f"  {python_probe}",
            "  exit /b 0",
            ")",
        ]
        if pdm_missing:
            body.extend(
                [
                    'if /I "%~nx1"=="pdm_shim.py" (',
                    '  >&2 echo No module named pdm',
                    "  exit /b 1",
                    ")",
                ]
            )
        if venv_fails:
            body.extend(
                [
                    'if "%~1"=="-m" if "%~2"=="venv" (',
                    '  >&2 echo Permission denied creating virtual environment',
                    "  exit /b 1",
                    ")",
                ]
            )
        body.extend(
            [
                '>&2 echo unexpected args: %*',
                "exit /b 2",
            ]
        )
        path = path.with_suffix(".cmd")
        path.write_text("\r\n".join(body) + "\r\n", encoding="utf-8")
        return

    body = [
        "#!/usr/bin/env bash",
        "if [[ \"${1:-}\" == \"-c\" ]]; then",
        (
            "  printf '%s\\n' "
            "'{\"executable\": \"FAKE_EXE\", \"version\": \"3.13.2\", "
            '\"prefix\": \"/fake\", \"base_prefix\": \"/fake\", \"real_prefix\": \"\", '
            '\"exec_prefix\": \"/fake\", \"base_exec_prefix\": \"/fake\", '
            '\"virtual_env\": \"\"}\' | sed \"s#FAKE_EXE#$0#\"'
        ),
        "  exit 0",
        "fi",
    ]
    if pdm_missing:
        body.extend(
            [
                "if [[ \"${1:-}\" == \"-m\" && \"${2:-}\" == \"pdm\" ]]; then",
                "  printf '%s\\n' \"No module named pdm\" >&2",
                "  exit 1",
                "fi",
            ]
        )
    if venv_fails:
        body.extend(
            [
                "if [[ \"${1:-}\" == \"-m\" && \"${2:-}\" == \"venv\" ]]; then",
                "  printf '%s\\n' \"Permission denied creating virtual environment\" >&2",
                "  exit 1",
                "fi",
            ]
        )
    body.extend(
        [
            "printf 'unexpected args: %s\\n' \"$*\" >&2",
            "exit 2",
        ]
    )
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def test_python_toolchain_happy_path_reports_selected_python() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    python_exe = _real_python_executable()
    proc = _run_tool(
        "resolve",
        "--repo-root",
        str(repo_root),
        "--workflow",
        "test",
        "--emit",
        "json",
        "--python-exe",
        python_exe,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["python"]["ok"] is True
    assert payload["python"]["path"]


def test_python_toolchain_rejects_windowsapps_alias_without_touching_fs() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    alias = r"C:\Users\tester\AppData\Local\Microsoft\WindowsApps\python.exe"
    proc = _run_tool(
        "resolve",
        "--repo-root",
        str(repo_root),
        "--workflow",
        "test",
        "--emit",
        "json",
        "--python-exe",
        alias,
        "--force-windows",
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["python"]["reason_code"] == "windowsapps_alias"
    assert "WindowsApps" in proc.stderr


def test_python_toolchain_reports_pdm_failure_with_one_diagnostic_path(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_python = tmp_path / "fake-python"
    _write_fake_python(fake_python, pdm_missing=True)

    fake_pdm = tmp_path / "pdm"
    if os.name == "nt":
        fake_pdm = fake_pdm.with_suffix(".cmd")
        fake_pdm.write_text(
            "@echo off\r\n>&2 echo Access is denied\r\nexit /b 1\r\n",
            encoding="utf-8",
        )
    else:
        fake_pdm.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' 'Access is denied' >&2\nexit 1\n",
            encoding="utf-8",
        )
        fake_pdm.chmod(fake_pdm.stat().st_mode | 0o111)

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    proc = _run_tool(
        "resolve",
        "--repo-root",
        str(repo_root),
        "--workflow",
        "doctor",
        "--emit",
        "json",
        "--python-exe",
        str(fake_python.with_suffix(".cmd") if os.name == "nt" else fake_python),
        "--require-pdm",
        env=env,
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["pdm"]["usable"] is False
    assert payload["pdm"]["attempts"]
    assert "Install pdm into this interpreter" in proc.stderr
    assert "tried:" in proc.stderr


def test_python_toolchain_can_fall_back_to_temp_venv_when_workspace_venv_creation_fails(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    python_exe = _real_python_executable()
    blocker = tmp_path / "blocked-parent"
    blocker.write_text("not a directory", encoding="utf-8")
    requested = blocker / ".venv"

    proc = _run_tool(
        "resolve",
        "--repo-root",
        str(repo_root),
        "--workflow",
        "offline_first_success",
        "--emit",
        "json",
        "--python-exe",
        python_exe,
        "--timeout-seconds",
        "30",
        "--ensure-venv",
        str(requested),
        "--allow-temp-venv-fallback",
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["venv"]["usable"] is True
    assert payload["venv"]["fallback_used"] is True
    assert payload["venv"]["source"] == "temp_fallback"
    venv_dir = payload["venv"]["dir"]
    assert venv_dir
    shutil.rmtree(venv_dir, ignore_errors=True)


def test_python_toolchain_reports_venv_creation_failure(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_python = tmp_path / "fake-python"
    _write_fake_python(fake_python, venv_fails=True)
    requested = tmp_path / ".venv"

    proc = _run_tool(
        "resolve",
        "--repo-root",
        str(repo_root),
        "--workflow",
        "offline_first_success",
        "--emit",
        "json",
        "--python-exe",
        str(fake_python.with_suffix(".cmd") if os.name == "nt" else fake_python),
        "--ensure-venv",
        str(requested),
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["venv"]["reason_code"] == "venv_create_failed"
    assert "Check write permissions" in proc.stderr
