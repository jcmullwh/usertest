from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sandbox_runner.diagnostics as diagnostics


@dataclass(frozen=True)
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_probe_commands_in_container_parses_stdout(monkeypatch: Any) -> None:
    prefix = ["docker", "exec", "-i", "c"]

    def fake_run(argv: list[str], **_kwargs: Any) -> _Proc:
        assert argv[: len(prefix)] == prefix
        return _Proc(returncode=0, stdout="git=1\npython=0\nuid=1000\n", stderr="warn\n")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)

    present, meta = diagnostics.probe_commands_in_container(
        command_prefix=prefix,
        commands=["git", "python"],
    )
    assert present == {"git": True, "python": False}
    assert meta["exit_code"] == 0
    assert meta["stderr"] == "warn"
    assert meta["uid"] == 1000


def test_probe_commands_in_container_is_best_effort(monkeypatch: Any) -> None:
    def boom(_argv: list[str], **_kwargs: Any) -> _Proc:
        raise OSError("boom")

    monkeypatch.setattr(diagnostics.subprocess, "run", boom)

    present, meta = diagnostics.probe_commands_in_container(command_prefix=["x"], commands=["git"])
    assert present == {}
    assert meta["error"] == "boom"


def test_capture_dns_snapshot_writes_file(tmp_path: Path, monkeypatch: Any) -> None:
    def fake_run(_argv: list[str], **_kwargs: Any) -> _Proc:
        return _Proc(returncode=7, stdout="out\n", stderr="err\n")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)

    diagnostics.capture_dns_snapshot(
        command_prefix=["docker", "exec", "-i", "c"],
        artifacts_dir=tmp_path,
    )
    text = (tmp_path / "dns_snapshot.txt").read_text(encoding="utf-8")
    assert "exit_code=7" in text
    assert "stdout:" in text
    assert "stderr:" in text


def test_capture_container_artifacts_scrubs_env_allowlist(tmp_path: Path, monkeypatch: Any) -> None:
    (tmp_path / "sandbox.json").write_text(
        json.dumps({"env_allowlist": ["KEY1", "OTHER"]}) + "\n",
        encoding="utf-8",
    )

    inspect_payload = [
        {
            "Config": {
                "Env": [
                    "KEY1=secret",
                    "KEY2=keep",
                    "OTHER=val",
                    "NOEQUALS",
                ]
            }
        }
    ]

    def fake_run(argv: list[str], **_kwargs: Any) -> _Proc:
        if argv[:2] == ["docker", "logs"]:
            return _Proc(returncode=0, stdout="log\n", stderr="")
        if argv[:2] == ["docker", "inspect"]:
            return _Proc(returncode=0, stdout=json.dumps(inspect_payload), stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)

    diagnostics.capture_container_artifacts(container_name="c", artifacts_dir=tmp_path)

    logs_text = (tmp_path / "container_logs.txt").read_text(encoding="utf-8")
    assert "exit_code=0" in logs_text
    assert "log" in logs_text

    scrubbed = json.loads((tmp_path / "container_inspect.json").read_text(encoding="utf-8"))
    env = scrubbed[0]["Config"]["Env"]
    assert "KEY1=<redacted>" in env
    assert "OTHER=<redacted>" in env
    assert "KEY2=keep" in env
    assert "NOEQUALS" in env
