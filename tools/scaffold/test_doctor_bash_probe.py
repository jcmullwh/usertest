from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_for_doctor_bash_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def _doctor_args() -> argparse.Namespace:
    return argparse.Namespace(skip_tool_checks=False, require_pip=False)


def test_doctor_uses_clean_bash_probe_and_retries_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    probe_calls: list[tuple[list[str], float]] = []
    bash_attempts = {"count": 0}

    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(scaffold, "_load_registry", lambda _: {})
    monkeypatch.setattr(scaffold, "_load_projects", lambda _: [])
    monkeypatch.setattr(scaffold, "_write_doctor_tool_report", lambda **_: None)
    monkeypatch.setattr(scaffold, "_eprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scaffold, "_probe_temp_writable", lambda *, timeout_seconds: (True, str(repo_root), None))

    def fake_which(name: str) -> str | None:
        if name == "git":
            return "/usr/bin/git"
        if name == "bash":
            return "/usr/bin/bash"
        return None

    monkeypatch.setattr(scaffold, "_which", fake_which)

    def fake_probe_tool_version(*, argv: list[str], timeout_seconds: float):
        probe_calls.append((argv, timeout_seconds))
        if argv == [sys.executable, "-m", "pip", "--version"]:
            return True, "pip ok", None
        if argv == ["/usr/bin/git", "--version"]:
            return True, "git version 2.53.0", None
        if argv == ["/usr/bin/bash", "--noprofile", "--norc", "-lc", "printf ok"]:
            bash_attempts["count"] += 1
            if bash_attempts["count"] == 1:
                return False, None, f"timed out after {timeout_seconds:.1f}s"
            return True, "ok", None
        return True, "ok", None

    monkeypatch.setattr(scaffold, "_probe_tool_version", fake_probe_tool_version)

    rc = scaffold.cmd_doctor(_doctor_args())
    assert rc == 0
    assert bash_attempts["count"] == 2
    assert ([ "/usr/bin/bash", "--noprofile", "--norc", "-lc", "printf ok"], 3.0) in probe_calls
