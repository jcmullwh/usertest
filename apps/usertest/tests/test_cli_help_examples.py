from __future__ import annotations

import pytest

from usertest.cli import build_parser


def test_usertest_help_includes_discovery_examples(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0

    out = capsys.readouterr().out
    assert "python -m usertest.cli personas list --repo-root ." in out
    assert "python -m usertest.cli missions list --repo-root ." in out


def test_personas_list_help_clarifies_repo_semantics(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["personas", "list", "--help"])
    assert excinfo.value.code == 0

    out = capsys.readouterr().out
    assert "Local path: read in-place." in out
    assert "Git URL: cloned to a temp dir." in out
    assert "usertest personas list --repo-root ." in out


def test_missions_list_help_clarifies_repo_semantics(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["missions", "list", "--help"])
    assert excinfo.value.code == 0

    out = capsys.readouterr().out
    assert "Local path: read in-place." in out
    assert "Git URL: cloned to a temp dir." in out
    assert "usertest missions list --repo-root ." in out

