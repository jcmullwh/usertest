from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from runner_core import RunnerConfig

import usertest_implement.cli as implement_cli


def test_verification_failure_blocks_commit_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "passed": False,
                "commands": [{"index": 1, "command": "echo nope", "exit_code": 1}],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_run_once(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("finalize_commit must not be called when verification fails")

    monkeypatch.setattr(implement_cli, "run_once", fake_run_once)
    monkeypatch.setattr(implement_cli, "finalize_commit", fail_if_called)

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text("# ticket\n", encoding="utf-8")

    parser = implement_cli.build_parser()
    args = parser.parse_args(
        [
            "run",
            "--ticket-path",
            str(ticket_path),
            "--repo",
            str(target_repo),
            "--no-docker",
            "--commit",
            "--verify-command",
            "echo ok",
            "--no-move-on-start",
            "--no-move-on-commit",
        ]
    )

    cfg = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        agents={},
        policies={},
    )
    selected = implement_cli.SelectedTicket(
        fingerprint="fp",
        ticket_id="T-1",
        title="Test ticket",
        export_kind=None,
        owner_root=None,
        idea_path=None,
        ticket_markdown="# ticket\n",
        tickets_export_path=None,
        export_index=None,
    )

    exit_code = implement_cli._run_selected_ticket(
        args=args,
        repo_root=tmp_path,
        cfg=cfg,
        selected=selected,
    )

    assert exit_code == 2
    assert (run_dir / "ticket_ref.json").exists()
    assert (run_dir / "timing.json").exists()

    captured = capsys.readouterr()
    assert captured.out.strip().splitlines()[-1] == str(run_dir)

