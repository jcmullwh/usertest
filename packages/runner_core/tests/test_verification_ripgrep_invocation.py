from __future__ import annotations

from pathlib import Path

import pytest

import runner_core.runner as runner_mod


class _Proc:
    def __init__(self, argv: list[str], *, returncode: int, stdout: str, stderr: str) -> None:
        self.args = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ripgrep_unexpected_arg_stderr(token: str) -> str:
    return (
        f"error: Found argument '{token}' which wasn't expected, or isn't valid in this context\n"
    )


@pytest.mark.parametrize("pattern", ["--skip-install", "--skip-install|--use-pythonpath"])
def test_verification_ripgrep_inserts_e_for_unexpected_leading_dash_pattern_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
) -> None:
    monkeypatch.setattr(runner_mod, "_is_windows", lambda: True)

    calls: list[list[str]] = []

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        calls.append(list(argv))
        if argv and argv[0] == "rg" and "-e" not in argv:
            token = next((t for t in argv[1:] if t.startswith("--")), argv[1])
            return _Proc(
                argv,
                returncode=2,
                stdout="",
                stderr=_ripgrep_unexpected_arg_stderr(token),
            )
        return _Proc(argv, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=[f"rg {pattern} README.md"],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary["passed"] is True
    assert len(calls) == 2
    assert calls[0][0] == "rg"
    assert calls[1][:3] == ["rg", "-e", pattern]

    cmd0 = summary["commands"][0]
    assert cmd0["argv"] == calls[1]
    assert cmd0["rewritten"] is True
    assert isinstance(cmd0.get("rewrite"), dict)
    assert cmd0["rewrite"]["kind"] == "ripgrep_unexpected_argument_to_regexp"


@pytest.mark.parametrize("pattern", ["--skip-install", "--skip-install|--use-pythonpath"])
def test_verification_ripgrep_inserts_e_inside_docker_command_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
) -> None:
    calls: list[list[str]] = []
    command_prefix = ["docker", "exec", "-i", "-w", "/workspace", "c"]

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        calls.append(list(argv))
        try:
            rg_idx = argv.index("rg")
        except ValueError:
            return _Proc(argv, returncode=0, stdout="ok\n", stderr="")
        inner = argv[rg_idx:]
        if "-e" not in inner:
            token = next((t for t in inner[1:] if t.startswith("--")), inner[1])
            return _Proc(
                argv,
                returncode=2,
                stdout="",
                stderr=_ripgrep_unexpected_arg_stderr(token),
            )
        return _Proc(argv, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path / "run",
        attempt_number=1,
        commands=[f"rg {pattern} README.md"],
        command_prefix=command_prefix,
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary["passed"] is True
    assert len(calls) == 2
    assert calls[0][: len(command_prefix)] == command_prefix
    assert calls[1][: len(command_prefix) + 3] == [*command_prefix, "rg", "-e", pattern]

    cmd0 = summary["commands"][0]
    assert cmd0["argv"] == calls[1]
    assert cmd0["rewritten"] is True
