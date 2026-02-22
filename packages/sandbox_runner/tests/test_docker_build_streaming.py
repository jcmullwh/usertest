from __future__ import annotations

import builtins
from pathlib import Path

import sandbox_runner.docker as docker


class _DummyProc:
    def __init__(self, *, lines: list[str], return_code: int) -> None:
        self.stdout = iter(lines)
        self._return_code = return_code
        self.wait_called = False

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        self.wait_called = True
        return self._return_code

    def kill(self) -> None:
        return


def test_docker_build_streaming_does_not_fail_when_print_raises_oserror(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dummy = _DummyProc(lines=["line-1\n", "line-2\n"], return_code=0)

    def _fake_popen(*_args, **_kwargs):
        return dummy

    def _failing_print(*_args, **_kwargs) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(docker.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(builtins, "print", _failing_print)

    log_path = tmp_path / "docker_build.log"
    rc = docker._docker_build_streaming(
        argv=["docker", "build", "--progress=plain", "-t", "t", "-f", "Dockerfile", "."],
        cwd=tmp_path,
        log_path=log_path,
    )

    assert rc == 0
    assert dummy.wait_called is True
    assert "line-1" in log_path.read_text(encoding="utf-8")
