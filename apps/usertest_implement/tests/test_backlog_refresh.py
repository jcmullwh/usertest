from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from usertest_implement import cli as implement_cli


def test_refresh_backlog_exports_ready_for_ticket_only(monkeypatch: object, tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "runs" / "usertest" / "usertest").mkdir(parents=True, exist_ok=True)

    calls: list[tuple[str, list[str]]] = []

    def _capture(argv: list[str], *, cwd: Path, label: str) -> None:
        assert cwd == repo_root
        calls.append((label, argv))

    monkeypatch.setattr(implement_cli, "_run_workflow_step", _capture)

    args = Namespace(
        backlog_runs_dir=None,
        backlog_target="usertest",
        backlog_agent="codex",
        backlog_model=None,
        review_agent=None,
        review_model=None,
    )

    implement_cli._refresh_backlog_for_ticket_implementation(args=args, repo_root=repo_root)

    export_calls = [argv for label, argv in calls if label == "reports export-tickets"]
    assert len(export_calls) == 1
    export_cmd = export_calls[0]
    assert "--stage" in export_cmd
    assert export_cmd[export_cmd.index("--stage") + 1] == "ready_for_ticket"
