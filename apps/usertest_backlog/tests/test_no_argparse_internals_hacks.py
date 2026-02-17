from __future__ import annotations

from pathlib import Path


def test_cli_modules_do_not_mutate_argparse_private_internals() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    cli_paths = [
        repo_root / "apps" / "usertest" / "src" / "usertest" / "cli.py",
        repo_root / "apps" / "usertest_backlog" / "src" / "usertest_backlog" / "cli.py",
    ]

    for path in cli_paths:
        source = path.read_text(encoding="utf-8")
        assert "_choices_actions" not in source
        assert "choices.pop(" not in source
