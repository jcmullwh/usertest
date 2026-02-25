from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from runner_core import find_repo_root

import usertest.cli
from usertest.cli import main


def test_batch_fails_before_running_when_tool_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)
    (target_repo / "package.json").write_text("{}", encoding="utf-8")

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: codex",
                "  policy: safe",
                "  persona_id: quickstart_sprinter",
                "  mission_id: first_output_smoke",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        usertest.cli.shutil,
        "which",
        lambda cmd: cmd if cmd in {"node", "npm"} else None,
    )

    def _fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd and cmd[0] == "npm":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout", 0)))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(usertest.cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        usertest.cli,
        "run_once",
        lambda *_args, **_kwargs: pytest.fail("run_once should not run after batch validation"),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--command-probe-timeout-seconds",
                "0.1",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert "env:" in out.err
    assert "npm" in out.err


def test_batch_validates_mission_ids_upfront(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: codex",
                "  policy: safe",
                "  persona_id: quickstart_sprinter",
                "  mission_id: does_not_exist",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        usertest.cli,
        "run_once",
        lambda *_args, **_kwargs: pytest.fail("run_once should not run after batch validation"),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert "Unknown mission id" in out.err
    assert "code=unknown_mission_id" in out.err
    assert "hint=" in out.err


def test_batch_validate_only_exits_zero_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: codex",
                "  policy: inspect",
                "  persona_id: quickstart_sprinter",
                "  mission_id: privacy_locked_run",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        usertest.cli,
        "run_once",
        lambda *_args, **_kwargs: pytest.fail("run_once should not run in --validate-only mode"),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
                "--validate-only",
            ]
        )
    assert exc.value.code == 0

    out = capsys.readouterr()
    assert "Batch validation passed" in out.err


def test_batch_print_requests_exits_zero_and_outputs_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: codex",
                "  policy: inspect",
                "  persona_id: quickstart_sprinter",
                "  mission_id: privacy_locked_run",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        usertest.cli,
        "run_once",
        lambda *_args, **_kwargs: pytest.fail("run_once should not run in --print-requests mode"),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
                "--exec-env",
                "OPENAI_API_KEY=shh",
                "--print-requests",
            ]
        )
    assert exc.value.code == 0

    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert isinstance(payload, list)
    assert payload and payload[0]["index"] == 0
    req = payload[0]["request"]
    assert req["repo"] == target_repo.as_posix()
    assert "exec_env" in req
    assert req["exec_env"] == ["OPENAI_API_KEY=<redacted>"]


def test_batch_invalid_yaml_is_concise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                "  - repo: 'x'",
                "    agent: codex",
                "    policy: inspect",
                "    persona_id: quickstart_sprinter",
                "    mission_id: privacy_locked_run",
                "    bad: [",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert str(targets_path) in out.err
    assert re.search(r":\d+:\d+", out.err) is not None
    assert re.search(r">\s*bad:\s*\[", out.err) is not None
    assert "Traceback" not in out.err


def test_batch_fails_fast_when_agent_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: claude",
                "  policy: inspect",
                "  persona_id: quickstart_sprinter",
                "  mission_id: privacy_locked_run",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(usertest.cli.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(
        usertest.cli,
        "run_once",
        lambda *_args, **_kwargs: pytest.fail("run_once should not run after batch validation"),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert "agent binary" in out.err
    assert "claude" in out.err
    assert "targets[0]" in out.err


def test_batch_reports_legacy_keys_and_other_errors_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: codex",
                "  policy: inspect",
                "  persona: quickstart_sprinter",
                "  persona_id: quickstart_sprinter",
                "  mission_id: privacy_locked_run",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: does_not_exist",
                "  policy: inspect",
                "  persona_id: quickstart_sprinter",
                "  mission_id: privacy_locked_run",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        usertest.cli,
        "run_once",
        lambda *_args, **_kwargs: pytest.fail("run_once should not run after batch validation"),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert "legacy keys" in out.err
    assert "unknown agent" in out.err
    assert "targets[0]" in out.err
    assert "targets[1]" in out.err
    assert "Traceback" not in out.err


def test_batch_non_list_targets_is_structured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                "  bad: not-a-list",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
                "--validate-only",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert "targets: expected a list" in out.err
    assert "Traceback" not in out.err
    assert "docs/reference/targets-yaml.md" in out.err


def test_batch_missing_repo_is_structured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                "- agent: codex",
                "  policy: safe",
                "  seed: 0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
                "--validate-only",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert "targets[0].repo is required" in out.err
    assert re.search(rf"{re.escape(str(targets_path))}:2:\d+", out.err) is not None
    assert "Traceback" not in out.err
    assert "docs/reference/targets-yaml.md" in out.err


def test_batch_invalid_seed_is_structured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"- repo: {target_repo.as_posix()!r}",
                "  agent: codex",
                "  policy: safe",
                "  seed: nope",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "batch",
                "--repo-root",
                str(repo_root),
                "--targets",
                str(targets_path),
                "--skip-command-probes",
                "--validate-only",
            ]
        )
    assert exc.value.code == 2

    out = capsys.readouterr()
    assert "Batch validation failed" in out.err
    assert "targets[0].seed" in out.err
    assert "integer" in out.err
    assert "Traceback" not in out.err
    assert "docs/reference/targets-yaml.md" in out.err
