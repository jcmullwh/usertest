from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, run_once


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _setup_runner_root(tmp_path: Path) -> Path:
    runner_root = tmp_path / "runner_root"
    _write(
        runner_root / "configs" / "catalog.yaml",
        "\n".join(
            [
                "version: 1",
                "personas_dirs:",
                "  - configs/personas",
                "missions_dirs:",
                "  - configs/missions",
                "prompt_templates_dir: configs/prompt_templates",
                "report_schemas_dir: configs/report_schemas",
                "defaults:",
                "  persona_id: p",
                "  mission_id: m",
                "",
            ]
        ),
    )
    _write(
        runner_root / "configs" / "personas" / "p.persona.md",
        "\n".join(["---", "id: p", "name: P", "extends: null", "---", "Persona", ""]),
    )
    _write(
        runner_root / "configs" / "missions" / "m.mission.md",
        "\n".join(
            [
                "---",
                "id: m",
                "name: M",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: t.prompt.md",
                "report_schema: s.schema.json",
                "---",
                "Mission",
                "",
            ]
        ),
    )
    _write(
        runner_root / "configs" / "prompt_templates" / "t.prompt.md",
        "\n".join(
            [
                "PROMPT",
                "",
                "## Preflight summary",
                "",
                "${preflight_summary_md}",
                "",
                "## Environment",
                "",
                "```json",
                "${environment_json}",
                "```",
                "",
            ]
        ),
    )
    _write(
        runner_root / "configs" / "report_schemas" / "s.schema.json",
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {
                    "ok": {"type": "string"},
                    "extensions": {"type": "object", "additionalProperties": True},
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
    return runner_root


def _setup_target_repo(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "README.md", "# hi\n")
    _write(target / "USERS.md", "# Users\n")
    subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return target


def _verification_command() -> str:
    return 'python -c "print(\'ok\')"'


def _run_broker_wrapper(*, run_dir: Path, workspace_dir: Path) -> subprocess.CompletedProcess[str]:
    client_root = run_dir / "verification_broker" / "client"
    if os.name == "nt":
        wrapper = client_root / "verify_client.ps1"
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(wrapper),
            ],
            cwd=str(workspace_dir),
            check=False,
            capture_output=True,
            text=True,
        )
    wrapper = client_root / "verify_client.sh"
    return subprocess.run(
        ["sh", str(wrapper)],
        cwd=str(workspace_dir),
        check=False,
        capture_output=True,
        text=True,
    )


def test_run_once_reuses_broker_verification_without_post_agent_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        run_dir = last_message_path.parent
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        broker = _run_broker_wrapper(
            run_dir=run_dir,
            workspace_dir=Path(str(kwargs["workspace_dir"])),
        )
        assert broker.returncode == 0, broker.stderr or broker.stdout
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
        policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
            verification_commands=(_verification_command(),),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 0
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["source"] == "broker_reuse"
    assert verification["reused"] is True
    assert verification["passed"] is True
    assert not (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "broker_reuse"
    assert reuse["selected_request_id"]


def test_run_once_falls_back_to_post_agent_rerun_when_broker_command_not_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
        policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
            verification_commands=(_verification_command(),),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 0
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["source"] == "post_agent_rerun"
    assert verification["reused"] is False
    assert verification["passed"] is True
    assert (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "post_agent_rerun"
    assert reuse["fallback_reason"] == "broker_not_requested"
