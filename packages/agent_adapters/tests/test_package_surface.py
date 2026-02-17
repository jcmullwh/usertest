from __future__ import annotations

import json

import pytest

import agent_adapters
from agent_adapters.cli import main


def test_package_exports_version() -> None:
    assert isinstance(agent_adapters.__version__, str)
    assert agent_adapters.__version__.strip()


def test_cli_version_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["version"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == agent_adapters.__version__


def test_cli_doctor_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_which(binary: str) -> str | None:
        if binary == "codex":
            return "/usr/bin/codex"
        if binary == "gemini":
            return "/usr/bin/gemini"
        return None

    monkeypatch.setattr("agent_adapters.cli.shutil.which", _fake_which)
    exit_code = main(["doctor", "--json"])
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["binaries"]["codex"] == "/usr/bin/codex"
    assert payload["binaries"]["claude"] is None
    assert payload["binaries"]["gemini"] == "/usr/bin/gemini"
    assert payload["available"] == ["codex", "gemini"]
    assert payload["missing"] == ["claude"]
