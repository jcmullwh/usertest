from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _write_fake_python(path: Path, *, healthy: bool) -> None:
    if healthy:
        body = """#!/usr/bin/env bash
if [[ \"${1:-}\" == \"-c\" ]]; then
  printf '{\"executable\": \"%s\", \"version\": \"3.13.2\"}\\n' \"$0\"
  exit 0
fi
printf 'unexpected args: %s\\n' \"$*\" >&2
exit 2
"""
    else:
        body = """#!/usr/bin/env bash
if [[ \"${1:-}\" == \"-c\" ]]; then
  printf '%s\\n%s\\n' \
    'Fatal Python error: init_fs_encoding' \
    'ModuleNotFoundError: No module named '\''encodings'\''' >&2
  exit 1
fi
printf 'unexpected args: %s\\n' \"$*\" >&2
exit 2
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _run_helper(
    *, repo_root: Path, resolve_root: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    command = (
        "source scripts/python_preflight.sh && "
        f"usertest_resolve_python {str(resolve_root)!r} "
        ">/tmp/usertest_python_stdout.txt && "
        "printf '%s|%s|%s\\n' "
        "\"${USERTEST_PYTHON_BIN}\" "
        "\"${USERTEST_PYTHON_SOURCE}\" "
        "\"${USERTEST_PYTHON_VERSION}\""
    )
    return subprocess.run(
        ["bash", "-c", command],
        cwd=str(repo_root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_python_preflight_sh_prefers_usertest_python_override(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fake_python = tmp_path / "preferred-python"
    _write_fake_python(fake_python, healthy=True)

    env = dict(os.environ)
    env["USERTEST_PYTHON"] = str(fake_python)

    repo_target = tmp_path / "repo"
    repo_target.mkdir()
    proc = _run_helper(repo_root=repo_root, resolve_root=repo_target, env=env)

    assert proc.returncode == 0, proc.stderr
    selected_bin, selected_source, selected_version = proc.stdout.strip().split("|", 2)
    assert selected_bin == str(fake_python)
    assert selected_source == "sandbox_env"
    assert selected_version == "3.13.2"



def test_python_preflight_sh_rejects_incomplete_override_and_falls_back(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    broken_python = tmp_path / "broken-python"
    _write_fake_python(broken_python, healthy=False)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fallback_python = bin_dir / "python"
    _write_fake_python(fallback_python, healthy=True)

    env = dict(os.environ)
    env["USERTEST_PYTHON"] = str(broken_python)
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    repo_target = tmp_path / "repo"
    repo_target.mkdir()
    proc = _run_helper(repo_root=repo_root, resolve_root=repo_target, env=env)

    assert proc.returncode == 0, proc.stderr
    selected_bin, selected_source, selected_version = proc.stdout.strip().split("|", 2)
    assert selected_bin == str(fallback_python)
    assert selected_source == "command_python"
    assert selected_version == "3.13.2"
