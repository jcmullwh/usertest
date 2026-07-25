from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest
from agent_adapters.codex_cli import CodexExecResult

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.retained_oracle_assets import (
    _sha256_json,
    nearest_existing_runs_ancestor,
    stage_retained_oracle_asset,
    validate_retained_oracle_asset_source,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _asset_fixture(tmp_path: Path) -> tuple[Path, dict[str, object], Path]:
    runs_root = tmp_path / "outer" / "runs"
    bundle = runs_root / "research" / "asset" / "bundle"
    replay = bundle / ".usertest_research" / "replay.py"
    _write(replay, "print('retained replay passed')\n")
    manifest = {
        ".usertest_research/replay.py": {
            "kind": "file",
            "mode": stat.S_IMODE(replay.stat().st_mode),
            "sha256": hashlib.sha256(replay.read_bytes()).hexdigest(),
            "size_bytes": replay.stat().st_size,
        }
    }
    asset = {
        "asset_id": "outcome_asset:"
        + _sha256_json({"schema_version": 1, "manifest": manifest}),
        "runs_relative_path": "research/asset/bundle",
        "manifest": manifest,
        "manifest_sha256": _sha256_json(manifest),
    }
    projection = {
        "schema_version": 1,
        "role": "original_scenario",
        "outcome_oracle_id": "outcome_oracle:" + "a" * 64,
        "oracle_kind": "staged_replay",
        "oracle_repo_revision": "b" * 40,
        "asset": asset,
    }
    spec: dict[str, object] = {
        **projection,
        "transport_sha256": _sha256_json(projection),
    }
    return runs_root, spec, replay


def test_nearest_runs_root_and_source_manifest_are_exact(tmp_path: Path) -> None:
    runs_root, spec, _ = _asset_fixture(tmp_path)
    export_path = runs_root / "backlog" / "run-1" / "tickets.json"
    _write(export_path, "{}\n")

    assert nearest_existing_runs_ancestor(export_path) == runs_root.resolve()
    source_root, manifest = validate_retained_oracle_asset_source(
        spec=spec,
        trusted_runs_root=runs_root,
    )
    assert source_root == (runs_root / "research" / "asset" / "bundle").resolve()
    assert list(manifest) == [".usertest_research/replay.py"]

    with pytest.raises(ValueError, match="runs_relative_path_unsafe"):
        forged = json.loads(json.dumps(spec))
        forged["asset"]["runs_relative_path"] = "../outside"
        unsigned = {key: value for key, value in forged.items() if key != "transport_sha256"}
        forged["transport_sha256"] = _sha256_json(unsigned)
        validate_retained_oracle_asset_source(
            spec=forged,
            trusted_runs_root=runs_root,
        )


def test_stage_is_excluded_before_copy_and_idempotent_for_exact_resume(
    tmp_path: Path,
) -> None:
    runs_root, spec, _ = _asset_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    _init_git_repo(workspace)
    _write(workspace / "tracked.txt", "tracked\n")
    _commit_all(workspace)

    first = stage_retained_oracle_asset(
        workspace=workspace,
        trusted_runs_root=runs_root,
        spec=spec,
    )
    assert first["reused_existing"] is False
    assert first["copied_paths"] == [".usertest_research/replay.py"]
    exclude = workspace / ".git" / "info" / "exclude"
    assert "/.usertest_research/replay.py" in exclude.read_text(encoding="utf-8")
    assert (
        subprocess.run(
            ["git", "status", "--porcelain", "--", ".usertest_research"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )

    second = stage_retained_oracle_asset(
        workspace=workspace,
        trusted_runs_root=runs_root,
        spec=spec,
    )
    assert second["reused_existing"] is True
    assert second["copied_paths"] == []


def test_stage_supports_worktree_git_file(tmp_path: Path) -> None:
    runs_root, spec, _ = _asset_fixture(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    external_git_dir = tmp_path / "common.git" / "worktrees" / "one"
    (external_git_dir / "info").mkdir(parents=True)
    _write(workspace / ".git", f"gitdir: {external_git_dir}\n")

    receipt = stage_retained_oracle_asset(
        workspace=workspace,
        trusted_runs_root=runs_root,
        spec=spec,
    )

    assert receipt["git_exclude_path"] == str(external_git_dir / "info" / "exclude")
    assert "/.usertest_research/replay.py" in (
        external_git_dir / "info" / "exclude"
    ).read_text(encoding="utf-8")


def test_tamper_and_collision_fail_without_overwrite(tmp_path: Path) -> None:
    runs_root, spec, replay = _asset_fixture(tmp_path)
    source_workspace = tmp_path / "source-tamper-workspace"
    _init_git_repo(source_workspace)
    replay.write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bundle_tampered"):
        stage_retained_oracle_asset(
            workspace=source_workspace,
            trusted_runs_root=runs_root,
            spec=spec,
        )
    assert not (source_workspace / ".usertest_research").exists()

    runs_root, spec, _ = _asset_fixture(tmp_path / "collision")
    collision_workspace = tmp_path / "collision-workspace"
    _init_git_repo(collision_workspace)
    existing = collision_workspace / ".usertest_research" / "unrelated.txt"
    _write(existing, "preserve me\n")
    with pytest.raises(ValueError, match="destination_tampered"):
        stage_retained_oracle_asset(
            workspace=collision_workspace,
            trusted_runs_root=runs_root,
            spec=spec,
        )
    assert existing.read_text(encoding="utf-8") == "preserve me\n"
    assert not (collision_workspace / ".usertest_research" / "replay.py").exists()


def _setup_runner_root(tmp_path: Path) -> Path:
    root = tmp_path / "runner-root"
    _write(
        root / "configs" / "catalog.yaml",
        "\n".join(
            [
                "version: 1",
                "personas_dirs: [configs/personas]",
                "missions_dirs: [configs/missions]",
                "prompt_templates_dir: configs/prompt_templates",
                "report_schemas_dir: configs/report_schemas",
                "defaults: {persona_id: p, mission_id: m}",
                "",
            ]
        ),
    )
    _write(
        root / "configs" / "personas" / "p.persona.md",
        "---\nid: p\nname: P\nextends: null\n---\nPersona\n",
    )
    _write(
        root / "configs" / "missions" / "m.mission.md",
        "---\nid: m\nname: M\nextends: null\nexecution_mode: single_pass_inline_report\n"
        "prompt_template: t.prompt.md\nreport_schema: s.schema.json\n---\nMission\n",
    )
    _write(
        root / "configs" / "prompt_templates" / "t.prompt.md",
        "PROMPT\n${preflight_summary_md}\n${environment_json}\n",
    )
    _write(
        root / "configs" / "report_schemas" / "s.schema.json",
        json.dumps(
            {
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
            }
        ),
    )
    return root


def _stub_agent(monkeypatch: pytest.MonkeyPatch, *, saw_workspace: list[Path]) -> None:
    monkeypatch.setattr(
        runner_mod,
        "_probe_commands_local",
        lambda commands, **kwargs: (
            {command: True for command in commands},
            {"command_probe_details": {}},
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "_probe_agent_cli_version",
        lambda **kwargs: {"ok": True, "argv": ["codex", "--version"], "exit_code": 0},
    )
    monkeypatch.setattr(
        runner_mod,
        "_agent_auth_present_local",
        lambda **kwargs: (True, "test_stub"),
    )

    def _fake_run_codex_exec(**kwargs: object) -> CodexExecResult:
        workspace = Path(str(kwargs["workspace_dir"]))
        saw_workspace.append(workspace)
        assert not (workspace / ".usertest_research").exists()
        raw_events = Path(str(kwargs["raw_events_path"]))
        last_message = Path(str(kwargs["last_message_path"]))
        stderr = Path(str(kwargs["stderr_path"]))
        raw_events.write_text(
            json.dumps({"id": "1", "msg": {"type": "agent_message", "message": "ok"}})
            + "\n",
            encoding="utf-8",
        )
        last_message.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return CodexExecResult(
            argv=["codex"],
            exit_code=0,
            raw_events_path=raw_events,
            last_message_path=last_message,
            stderr_path=stderr,
        )

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)


def test_run_once_stages_current_replay_after_agent_and_disables_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root, spec, _ = _asset_fixture(tmp_path)
    target = tmp_path / "target"
    _init_git_repo(target)
    _write(target / "README.md", "target\n")
    _write(target / "USERS.md", "users\n")
    _commit_all(target)
    saw_workspace: list[Path] = []
    _stub_agent(monkeypatch, saw_workspace=saw_workspace)

    result = run_once(
        RunnerConfig(
            repo_root=_setup_runner_root(tmp_path),
            runs_dir=tmp_path / "runner-runs",
            agents={"codex": {"binary": "codex"}},
            policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
        ),
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
            agent_append_system_prompt="# Ticket context\n\nimmutable ticket blob",
            supervisor_instruction="Do not invoke Docker or delete files.",
            keep_workspace=True,
            verification_commands=("python .usertest_research/replay.py",),
            verification_reuse_mode="off",
            retained_oracle_assets_root=runs_root,
            retained_oracle_asset_spec=spec,
        ),
    )

    assert result.exit_code == 0
    assert len(saw_workspace) == 1
    workspace = saw_workspace[0]
    assert (workspace / ".usertest_research" / "replay.py").is_file()
    staging = json.loads(
        (result.run_dir / "retained_oracle_asset_staging.json").read_text(encoding="utf-8")
    )
    assert staging["validated_immediately_before_dispatch"] is True
    append_prompt = (result.run_dir / "agent_prompts" / "append_system_prompt.md").read_text(
        encoding="utf-8"
    )
    assert "only after a successful agent/report turn" in append_prompt
    assert "Do not create, edit, or delete `.usertest_research`" in append_prompt
    assert append_prompt.index("immutable ticket blob") < append_prompt.index(
        "Runner-owned retained research asset boundary"
    )
    assert append_prompt.index("Runner-owned retained research asset boundary") < (
        append_prompt.index("Runner-owned supervisor execution constraints")
    )
    assert "Do not invoke Docker or delete files." in append_prompt
    target_ref = json.loads((result.run_dir / "target_ref.json").read_text(encoding="utf-8"))
    assert target_ref["retained_oracle_asset_transport"] == {
        "trusted_runs_root": str(runs_root.resolve()),
        "spec": spec,
    }
    assert target_ref["supervisor_instruction"] == (
        "Do not invoke Docker or delete files."
    )
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert attempts["attempts"][0]["verification"]["broker_requested"] is False
    assert not (result.run_dir / "verification_broker").exists()


def test_missing_bundle_and_auto_reuse_fail_before_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root, spec, _ = _asset_fixture(tmp_path)
    missing_spec = json.loads(json.dumps(spec))
    missing_spec["asset"]["runs_relative_path"] = "research/asset/missing-bundle"
    missing_unsigned = {
        key: value for key, value in missing_spec.items() if key != "transport_sha256"
    }
    missing_spec["transport_sha256"] = _sha256_json(missing_unsigned)
    called = False

    def _unexpected_agent(**kwargs: object) -> CodexExecResult:
        nonlocal called
        called = True
        raise AssertionError("agent must not run")

    monkeypatch.setattr(runner_mod, "run_codex_exec", _unexpected_agent)
    config = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runner-runs",
        agents={},
        policies={"write": {"codex": {"allow_edits": True}}},
    )
    with pytest.raises(ValueError, match="bundle_missing"):
        run_once(
            config,
            RunRequest(
                repo=str(tmp_path),
                verification_reuse_mode="off",
                retained_oracle_assets_root=runs_root,
                retained_oracle_asset_spec=missing_spec,
            ),
        )
    assert called is False

    _, valid_spec, _ = _asset_fixture(tmp_path / "valid")
    valid_root = tmp_path / "valid" / "outer" / "runs"
    with pytest.raises(ValueError, match="requires_verification_reuse_off"):
        run_once(
            config,
            RunRequest(
                repo=str(tmp_path),
                verification_reuse_mode="auto",
                retained_oracle_assets_root=valid_root,
                retained_oracle_asset_spec=valid_spec,
            ),
        )
    assert called is False


def test_ordinary_run_request_has_no_asset_transport() -> None:
    request = RunRequest(repo="target")
    assert request.retained_oracle_assets_root is None
    assert request.retained_oracle_asset_spec is None
