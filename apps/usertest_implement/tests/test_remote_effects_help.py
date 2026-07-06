from __future__ import annotations

import json
from pathlib import Path

import pytest

from usertest_implement.cli import build_parser, main


def test_run_help_mentions_settings_loaded_remote_write_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["run", "--help"])
    assert excinfo.value.code == 0

    out = capsys.readouterr().out
    normalized = " ".join(out.split())
    assert "auto-loaded default settings profile enables it unless --no-commit" in normalized
    assert "auto-loaded default settings profile enables it unless --no-push" in normalized
    assert "auto-loaded default settings profile enables it unless --no-pr" in normalized
    assert "do not run the agent, commit, push, create PRs, or move tickets" in normalized


def test_run_dry_run_prints_settings_loaded_commit_push_pr_defaults(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner_root = tmp_path / "target"
    tickets_dir = owner_root / ".agents" / "plans" / "2 - ready"
    tickets_dir.mkdir(parents=True)
    ticket_path = tickets_dir / "20260706_0777f1b4e2df137b_remote-effects.md"
    ticket_path.write_text(
        "# Define and validate CLI remote-effect boundaries\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `0777f1b4e2df137b`\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--repo-root",
                str(Path(__file__).resolve().parents[3]),
                "run",
                "--ticket-path",
                str(ticket_path),
                "--dry-run",
            ]
        )
    assert excinfo.value.code == 0

    payload = json.loads(capsys.readouterr().out)
    request = payload["run_request"]
    assert request["commit"] is True
    assert request["push"] is True
    assert request["pr"] is True
    assert payload["settings"]["auto_loaded"] is True
    assert payload["settings"]["applied"]["commit"] is True
    assert payload["settings"]["applied"]["push"] is True
    assert payload["settings"]["applied"]["pr"] is True
