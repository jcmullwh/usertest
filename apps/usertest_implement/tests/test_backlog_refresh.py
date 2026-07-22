from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import usertest_implement.commands.run as run_commands


def test_refresh_backlog_exports_ready_for_ticket_only(monkeypatch: object, tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "runs" / "usertest" / "usertest").mkdir(parents=True, exist_ok=True)

    requests: list[object] = []

    def _capture(request: object) -> Path:
        requests.append(request)
        return (
            repo_root
            / "runs"
            / "usertest"
            / "usertest"
            / "_compiled"
            / "usertest.tickets_export.json"
        )

    monkeypatch.setattr(run_commands, "run_shadow_backlog_refresh", _capture)

    args = Namespace(
        backlog_runs_dir=None,
        backlog_target="usertest",
        backlog_agent="codex",
        backlog_model=None,
        review_agent=None,
        review_model=None,
        backlog_research_ref="origin/dev",
        backlog_breadth_profile="internal_maintenance",
        backlog_actions_yaml=None,
        backlog_atom_actions_yaml=None,
    )

    run_commands._refresh_backlog_for_ticket_implementation(args=args, repo_root=repo_root)

    assert len(requests) == 1
    request = requests[0]
    assert request.research_ref == "origin/dev"
    assert request.breadth_profile == "internal_maintenance"
    assert request.agent == "codex"
    assert request.target == "usertest"
