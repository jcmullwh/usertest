from __future__ import annotations

import pytest

from usertest_backlog.cli import build_parser


def test_parser_smoke_backlog_commands() -> None:
    parser = build_parser()
    args = parser.parse_args(["reports", "intent-snapshot", "--target", "x"])
    assert args.target == "x"

    args = parser.parse_args(["reports", "review-ux", "--target", "x", "--dry-run"])
    assert args.target == "x"
    assert args.dry_run is True

    args = parser.parse_args(["reports", "export-tickets", "--target", "x"])
    assert args.target == "x"

    args = parser.parse_args(["reports", "backlog", "--target", "x", "--dry-run"])
    assert args.target == "x"
    assert args.dry_run is True

    args = parser.parse_args(["reports", "compile", "--target", "x"])
    assert args.target == "x"

    args = parser.parse_args(["triage-prs", "--in", "prs.json"])
    assert args.input_json.name == "prs.json"

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--repo", "C:\\tmp\\x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["batch", "--targets", "configs\\targets.yaml"])
    with pytest.raises(SystemExit):
        parser.parse_args(["matrix", "plan", "--spec", "spec.yaml"])
    with pytest.raises(SystemExit):
        parser.parse_args(["lint", "--repo", "C:\\tmp\\x"])

    with pytest.raises(SystemExit):
        parser.parse_args(["reports", "not-a-command"])
