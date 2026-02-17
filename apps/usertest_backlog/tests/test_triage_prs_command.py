from __future__ import annotations

import json
from pathlib import Path

import pytest

from usertest_backlog.cli import main


def test_triage_prs_writes_json_and_markdown(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "pr_list.json"
    out_json = tmp_path / "triage_prs.json"
    out_md = tmp_path / "triage_prs.md"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "triage-prs",
                "--in",
                str(fixture),
                "--out-json",
                str(out_json),
                "--out-md",
                str(out_md),
            ]
        )
    assert exc.value.code == 0

    assert out_json.exists()
    assert out_md.exists()

    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["pull_requests_total"] == 3
    assert payload["clusters_total"] == 2
    sizes = [cluster["size"] for cluster in payload["clusters"]]
    assert sorted(sizes, reverse=True) == [2, 1]

    markdown = out_md.read_text(encoding="utf-8")
    assert "PR Triage Report" in markdown
    assert "PR #101" in markdown
    assert "PR #102" in markdown
