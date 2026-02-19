from __future__ import annotations

from pathlib import Path

from runner_core.runner import _gemini_include_directories_for_workspace


def test_gemini_include_directories_empty_when_runs_missing(tmp_path: Path) -> None:
    assert _gemini_include_directories_for_workspace(workspace_dir=tmp_path) == []


def test_gemini_include_directories_includes_runs_usertest_when_present(tmp_path: Path) -> None:
    (tmp_path / "runs" / "usertest").mkdir(parents=True, exist_ok=True)
    assert _gemini_include_directories_for_workspace(workspace_dir=tmp_path) == [
        str(Path("runs") / "usertest")
    ]


def test_gemini_include_directories_creates_runs_usertest_for_runner_repo(tmp_path: Path) -> None:
    (tmp_path / "tools" / "scaffold").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "scaffold" / "monorepo.toml").write_text("", encoding="utf-8")

    expected = [str(Path("runs") / "usertest")]
    assert _gemini_include_directories_for_workspace(workspace_dir=tmp_path) == expected
    assert (tmp_path / "runs" / "usertest").is_dir()
