from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from usertest_implement.batch_failure import classify_run_outcome
from usertest_implement.batch_runner import (
    BacklogSource,
    BatchCandidate,
    _add_batch_resource_conflicts,
    _batch_subprocess_env,
    _build_docker_resource_plan,
    _collect_wave_candidates,
    _pick_launchable_candidate_index,
    _refresh_backlog,
    _write_batch_token_monitoring_artifacts,
    _write_stream,
    run_batch,
)
from usertest_implement.batch_state import (
    build_initial_state,
    docker_resource_plan_path,
    persist_state,
    summary_path,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_maintenance_docker_config(repo_root: Path, *, cleanup_on_prepare: bool = True) -> None:
    path = repo_root / "configs" / "maintenance_docker.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "maintenance_docker:",
                '  local_image_repo: "usertest-maintenance"',
                '  published_image_repo: "ghcr.io/example/usertest-maintenance"',
                '  pull_policy: "if_missing"',
                '  seed_root: "/opt/usertest_maint_seed"',
                '  cache_root_subdir: "usertest_maint_venvs"',
                "  cleanup_enabled: true",
                "  keep_local_count: 5",
                "  keep_local_days: 7",
                "  keep_branch_alias_tags: true",
                "  protect_tags: []",
                f"  cleanup_on_prepare: {str(cleanup_on_prepare).lower()}",
                "  cleanup_dry_run_default: false",
                "  publish_branches:",
                '    - "dev"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_run_settings(
    repo_root: Path,
    *,
    exec_backend: str = "docker",
    exec_docker_profile: str | None = None,
    exec_cache: str = "warm",
    maintenance_venv_cache: bool = True,
) -> Path:
    path = repo_root / "configs" / "usertest_implement_settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    profile_line = (
        "      exec_docker_profile: null"
        if exec_docker_profile is None
        else f"      exec_docker_profile: {exec_docker_profile}"
    )
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "default_profile: default",
                "profiles:",
                "  default:",
                "    run_common:",
                f"      exec_backend: {exec_backend}",
                profile_line,
                f"      exec_cache: {exec_cache}",
                f"      maintenance_venv_cache: {str(maintenance_venv_cache).lower()}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_default_docker_resource_plan_records_current_unsafe_resources(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    settings_path = _write_run_settings(tmp_path)
    _write_maintenance_docker_config(tmp_path)

    plan = _build_docker_resource_plan(
        repo_root=tmp_path,
        exec_backend="docker",
        run_settings_path=settings_path,
        run_settings_profile="default",
        repo_input=str(tmp_path),
    )

    assert plan is not None
    assert plan["docker_profile"] == "maintenance"
    assert plan["configured_docker_profile"] is None
    assert plan["warm_cache"] is True
    assert plan["cache_mode"] == "warm"
    assert plan["maintenance_venv_cache"] is True
    assert plan["maintenance_venv_cache_configured"] is True
    assert plan["cleanup_on_prepare"] is True
    assert plan["pre_resolved_image_available"] is False
    assert plan["parallel_safe"] is False
    assert plan["scheduler_guard"]["conflict_key"] == "batch_resource:docker"
    assert [reason["reason_id"] for reason in plan["unsafe_reasons"]] == [
        "per_ticket_image_resolution",
        "cleanup_on_prepare",
        "writable_shared_maintenance_venv_mounts",
    ]


def test_local_backend_has_no_docker_resource_plan(tmp_path: Path, monkeypatch: Any) -> None:
    settings_path = _write_run_settings(tmp_path, exec_backend="local")

    plan = _build_docker_resource_plan(
        repo_root=tmp_path,
        exec_backend="local",
        run_settings_path=settings_path,
        run_settings_profile="default",
        repo_input=str(tmp_path),
    )

    assert plan is None

    config_path = tmp_path / "batch.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "defaults:",
                "  worker_roster:",
                "    - agent: codex",
                "phases:",
                "  - name: phase",
                "    sources:",
                "      - name: src",
                "        runs_dir: runs/src",
                "        target: usertest",
                "    severities: [high]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def _preflight(**kwargs: object) -> dict[str, object]:
        captured["exec_backend"] = kwargs.get("exec_backend")
        return {
            "head_sha": "abc123",
            "branch": "dev",
            "base_ci_run_url": None,
            "blockers": [{"blocker_id": "preflight", "class": "preflight"}],
        }

    monkeypatch.setattr("usertest_implement.batch_runner.run_batch_preflight", _preflight)

    assert run_batch(repo_root=tmp_path, config_path=config_path) == 2

    batch_dirs = sorted((tmp_path / "runs" / "_batch" / "usertest_implement").iterdir())
    persisted_state = json.loads((batch_dirs[-1] / "batch_state.json").read_text(encoding="utf-8"))
    assert captured["exec_backend"] == "local"
    assert "docker_resource_plan" not in persisted_state
    assert not docker_resource_plan_path(batch_dirs[-1]).exists()


def test_docker_resource_plan_is_rendered_to_batch_state_and_artifacts(tmp_path: Path) -> None:
    plan = {
        "schema_version": 1,
        "parallel_safe": False,
        "unsafe_reasons": [{"reason_id": "per_ticket_image_resolution"}],
    }
    batch_dir = tmp_path / "batch"
    state = build_initial_state(
        batch_id="20260709T000000Z",
        batch_commit="abc123",
        batch_branch="dev",
        base_ci_run_url=None,
        workers=[{"worker_index": 1, "agent": "codex", "model": None}],
        docker_resource_plan=plan,
    )

    persist_state(batch_dir, state)

    persisted_state = json.loads((batch_dir / "batch_state.json").read_text(encoding="utf-8"))
    persisted_summary = json.loads(summary_path(batch_dir).read_text(encoding="utf-8"))
    persisted_plan = json.loads(docker_resource_plan_path(batch_dir).read_text(encoding="utf-8"))
    assert persisted_state["docker_resource_plan"] == plan
    assert persisted_summary["docker_resource_plan"] == plan
    assert persisted_plan == plan


def test_pick_launchable_candidate_index_respects_conflict_keys(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ticket_path = owner_root / ".agents" / "plans" / "2 - ready" / "ticket.md"
    queue = [
        BatchCandidate(
            source_name="src",
            export_path=tmp_path / "export.json",
            fingerprint="aaaaaaaaaaaaaaaa",
            severity="high",
            title="Broker fix",
            owner_root=owner_root,
            ticket_path=ticket_path,
            execution_domain="runner_core",
            execution_conflict_keys=(
                "execution_domain:runner_core",
                "subsystem:verification_broker",
            ),
        ),
        BatchCandidate(
            source_name="src",
            export_path=tmp_path / "export.json",
            fingerprint="bbbbbbbbbbbbbbbb",
            severity="high",
            title="Docs fix",
            owner_root=owner_root,
            ticket_path=ticket_path,
            execution_domain="docs_onboarding",
            execution_conflict_keys=("execution_domain:docs_onboarding",),
        ),
    ]

    index = _pick_launchable_candidate_index(
        queue,
        active_conflict_keys={"execution_domain:runner_core"},
    )

    assert index == 1


def test_docker_batch_resource_conflict_serializes_ticket_runs(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ticket_path = owner_root / ".agents" / "plans" / "2 - ready" / "ticket.md"
    queue = [
        _add_batch_resource_conflicts(
            BatchCandidate(
                source_name="src",
                export_path=tmp_path / "export.json",
                fingerprint="aaaaaaaaaaaaaaaa",
                severity="high",
                title="First",
                owner_root=owner_root,
                ticket_path=ticket_path,
                execution_domain="runner_core",
                execution_conflict_keys=("ticket:aaaaaaaaaaaaaaaa",),
            ),
            exec_backend="docker",
        ),
        _add_batch_resource_conflicts(
            BatchCandidate(
                source_name="src",
                export_path=tmp_path / "export.json",
                fingerprint="bbbbbbbbbbbbbbbb",
                severity="high",
                title="Second",
                owner_root=owner_root,
                ticket_path=ticket_path,
                execution_domain="docs",
                execution_conflict_keys=("ticket:bbbbbbbbbbbbbbbb",),
            ),
            exec_backend="docker",
        ),
    ]

    assert queue[0].execution_conflict_keys == (
        "ticket:aaaaaaaaaaaaaaaa",
        "batch_resource:docker",
    )
    assert _pick_launchable_candidate_index(
        queue,
        active_conflict_keys={"batch_resource:docker"},
    ) is None


def test_classify_run_outcome_detects_registry_json_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "bootstrap_pip.log").write_text(
        "\n".join(
            [
                "USERTEST_GITLAB_INDEX_URL=https://gitlab.com/api/v4/projects/123/packages/pypi/simple",
                "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
            ]
        ),
        encoding="utf-8",
    )

    failure = classify_run_outcome(run_dir=run_dir, handoff_summary=None)

    assert failure["failure_class"] == "registry_or_auth"
    assert failure["global_blocker"] is True


def test_classify_run_outcome_detects_missing_broker_response(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    requests_dir = run_dir / "verification_broker" / "attempt1" / "requests"
    requests_dir.mkdir(parents=True)
    (requests_dir / "req_01.json").write_text(
        json.dumps({"request_id": "req_01"}),
        encoding="utf-8",
    )

    failure = classify_run_outcome(run_dir=run_dir, handoff_summary=None)

    assert failure["failure_class"] == "verification_control_plane"
    assert failure["global_blocker"] is True


def test_classify_run_outcome_marks_red_pr_as_ticket_regression(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/1",
            "ci_status": "failure",
            "ci_run_url": "https://example.invalid/run/1",
            "final_status": "failure",
        },
    )

    assert failure["failure_class"] == "ticket_regression"
    assert failure["global_blocker"] is True


def test_classify_run_outcome_marks_completed_failure_ci_as_ticket_regression(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "bootstrap_pip.log").write_text(
        "\n".join(
            [
                "$ docker exec ...",
                (
                    '    echo "Missing '
                    'GITLAB_PYPI_USERNAME/GITLAB_PYPI_PASSWORD '
                    'in container env." 1>&2'
                ),
                "exit_code=0",
            ]
        ),
        encoding="utf-8",
    )

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/2",
            "ci_status": "completed",
            "ci_conclusion": "failure",
            "ci_run_url": "https://example.invalid/run/2",
            "final_status": "success",
        },
    )

    assert failure["failure_class"] == "ticket_regression"
    assert failure["global_blocker"] is True


def test_classify_run_outcome_accepts_local_exercise_success(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "verification.json").write_text(
        json.dumps({"passed": True}),
        encoding="utf-8",
    )

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "pr_created": False,
            "ci_required": False,
            "final_status": "success",
        },
    )

    assert failure["failure_class"] == "success"
    assert failure["global_blocker"] is False


def test_classify_run_outcome_rejects_missing_requested_pr(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "verification.json").write_text(
        json.dumps({"passed": True}),
        encoding="utf-8",
    )

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "commit_requested": True,
            "commit_performed": False,
            "push_requested": True,
            "pushed": False,
            "pr_requested": True,
            "pr_created": False,
            "ci_required": False,
            "final_status": "success",
        },
    )

    assert failure["failure_class"] == "ticket_regression"
    assert failure["global_blocker"] is False
    assert "commit" in failure["summary"].lower()


def test_batch_subprocess_env_includes_repo_src_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "apps" / "usertest_backlog" / "src").mkdir(parents=True)
    (tmp_path / "packages" / "backlog_core" / "src").mkdir(parents=True)
    (tmp_path / "packages" / "run_artifacts" / "src").mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", f"existing_one{os.pathsep}existing_two")

    env = _batch_subprocess_env(tmp_path)

    assert env["PYTHONPATH"] == os.pathsep.join(
        [
            "apps/usertest_backlog/src",
            "packages/backlog_core/src",
            "packages/run_artifacts/src",
            "existing_one",
            "existing_two",
        ]
    )


def test_write_stream_ignores_oserror_from_closed_pipe() -> None:
    class BrokenStream:
        encoding = "utf-8"

        def write(self, text: str) -> Any:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise AssertionError("flush should not be called after a write failure")

    _write_stream(BrokenStream(), "hello")


def test_write_batch_token_monitoring_artifacts_writes_context(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch"
    _write_json(
        batch_dir / "batch_summary.json",
        {"status": "blocked", "completed_count": 0, "failed_count": 1},
    )
    _write_json(batch_dir / "batch_state.json", {"status": "blocked", "completed": []})
    _write_json(batch_dir / "global_blockers.json", {"global_blockers": [{"run_dir": None}]})
    _write_jsonl(batch_dir / "ticket_outcomes.jsonl", [{"run_dir": None}])

    _write_batch_token_monitoring_artifacts(batch_dir)

    assert (batch_dir / "token_batch_context.json").exists()
    assert (batch_dir / "token_batch_context.md").exists()
    assert not (batch_dir / "token_batch_context_error.json").exists()


def test_write_batch_token_monitoring_artifacts_is_non_fatal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()

    def _raise(_batch_dir: Path) -> dict[str, object]:
        raise RuntimeError("monitor failed")

    monkeypatch.setitem(
        sys.modules,
        "token_monitoring",
        SimpleNamespace(write_batch_context=_raise),
    )

    _write_batch_token_monitoring_artifacts(batch_dir)

    payload = json.loads((batch_dir / "token_batch_context_error.json").read_text())
    assert payload["non_fatal"] is True
    assert payload["type"] == "RuntimeError"


def test_run_batch_writes_token_context_for_preflight_blocker(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config_path = tmp_path / "batch.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "defaults:",
                "  worker_roster:",
                "    - agent: codex",
                "phases:",
                "  - name: phase",
                "    sources:",
                "      - name: src",
                "        runs_dir: runs/src",
                "        target: usertest",
                "    severities: [high]",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "usertest_implement.batch_runner.run_batch_preflight",
        lambda **_: {
            "head_sha": "abc123",
            "branch": "dev",
            "base_ci_run_url": None,
            "blockers": [{"blocker_id": "preflight", "class": "preflight"}],
        },
    )

    assert run_batch(repo_root=tmp_path, config_path=config_path) == 2

    batch_dirs = sorted((tmp_path / "runs" / "_batch" / "usertest_implement").iterdir())
    assert batch_dirs
    assert (batch_dirs[-1] / "token_batch_context.json").exists()


def test_refresh_backlog_uses_normal_export_dedupe_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def _capture(argv: list[str], **_: Any) -> None:
        calls.append(argv)

    monkeypatch.setattr("usertest_implement.batch_runner._run_logged_command", _capture)

    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    _refresh_backlog(
        repo_root=tmp_path,
        source=source,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        agent="codex",
        model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
    )

    export_calls = [argv for argv in calls if "export-tickets" in argv]
    assert len(export_calls) == 1
    export_cmd = export_calls[0]
    assert "--stage" in export_cmd
    assert export_cmd[export_cmd.index("--stage") + 1] == "ready_for_ticket"
    assert "--include-actioned" not in export_cmd
    assert "--skip-plan-folder-dedupe" not in export_cmd


def test_collect_wave_candidates_prefers_ready_queue_over_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ready_dir = tmp_path / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)
    ticket_path = ready_dir / "20260314_deadbeefdeadbeef_ticket.md"
    ticket_path.write_text(
        "\n".join(
            [
                "# Queue-first ticket",
                "",
                "- Fingerprint: `deadbeefdeadbeef`",
                "- Execution domain: `runner_core`",
                (
                    "- Execution conflict keys: "
                    "`execution_domain:runner_core`, "
                    "`subsystem:python_runtime`"
                ),
                "- Export kind: `implementation`",
                "- Stage: `ready_for_ticket`",
                "- Severity: `blocker`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    monkeypatch.setattr(
        "usertest_implement.batch_runner._refresh_backlog",
        lambda **_: (_ for _ in ()).throw(AssertionError("refresh should be skipped")),
    )

    candidates = _collect_wave_candidates(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        refresh_agent="codex",
        refresh_model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
        sources=[source],
        severities={"blocker", "high"},
        processed=set(),
        refresh_state={},
    )

    assert [candidate.fingerprint for candidate in candidates] == ["deadbeefdeadbeef"]
    assert candidates[0].ticket_path == ticket_path.resolve()
    assert candidates[0].execution_domain == "runner_core"
    assert candidates[0].execution_conflict_keys == (
        "execution_domain:runner_core",
        "subsystem:python_runtime",
    )


def test_collect_wave_candidates_skips_refresh_while_other_ready_work_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ready_dir = tmp_path / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)
    (ready_dir / "20260314_feedfacefeedface_ticket.md").write_text(
        "\n".join(
            [
                "# Medium ticket",
                "",
                "- Fingerprint: `feedfacefeedface`",
                "- Export kind: `implementation`",
                "- Stage: `ready_for_ticket`",
                "- Severity: `medium`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    monkeypatch.setattr(
        "usertest_implement.batch_runner._refresh_backlog",
        lambda **_: (_ for _ in ()).throw(AssertionError("refresh should be skipped")),
    )

    candidates = _collect_wave_candidates(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        refresh_agent="codex",
        refresh_model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
        sources=[source],
        severities={"blocker", "high"},
        processed=set(),
        refresh_state={},
    )

    assert candidates == []


def test_collect_wave_candidates_uses_per_ticket_fallback_when_conflict_metadata_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ready_dir = tmp_path / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)
    ticket_path = ready_dir / "20260314_facefeedfacefeed_ticket.md"
    ticket_path.write_text(
        "\n".join(
            [
                "# Missing conflict metadata",
                "",
                "- Fingerprint: `facefeedfacefeed`",
                "- Export kind: `implementation`",
                "- Stage: `ready_for_ticket`",
                "- Severity: `high`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    monkeypatch.setattr(
        "usertest_implement.batch_runner._refresh_backlog",
        lambda **_: (_ for _ in ()).throw(AssertionError("refresh should be skipped")),
    )

    candidates = _collect_wave_candidates(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        refresh_agent="codex",
        refresh_model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
        sources=[source],
        severities={"blocker", "high"},
        processed=set(),
        refresh_state={},
    )

    assert [candidate.fingerprint for candidate in candidates] == ["facefeedfacefeed"]
    assert candidates[0].execution_domain == "unknown"
    assert candidates[0].execution_conflict_keys == ("ticket:facefeedfacefeed",)
