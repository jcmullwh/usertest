from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

import usertest_backlog.triage_backlog as triage_backlog_mod
from usertest_backlog.cli import main


class _DeterministicEmbedder:
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            if any(token in lowered for token in ("parser", "malformed", "json", "triage")):
                vectors.append([1.0, 0.0, 0.0])
            elif any(token in lowered for token in ("docs", "readme", "install", "windows")):
                vectors.append([0.0, 1.0, 0.0])
            elif any(token in lowered for token in ("snapshot", "shell", "warning")):
                vectors.append([0.0, 0.0, 1.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


def test_triage_backlog_writes_expected_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "sample_issue_backlog.json"
    out_json = tmp_path / "triage_backlog.json"
    out_md = tmp_path / "triage_backlog.md"

    monkeypatch.setattr(triage_backlog_mod, "get_default_embedder", _DeterministicEmbedder)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "triage-backlog",
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
    assert {"themes", "dedupe_clusters", "totals", "config"} <= set(payload.keys())

    totals = payload["totals"]
    assert totals["issues_total"] == 6
    assert totals["dedupe_clusters_total"] >= 1
    assert totals["theme_clusters_total"] >= 1

    themes = payload["themes"]
    assert any(int(theme["size"]) >= 2 for theme in themes)
    assert any(int(theme["groups_count"]) == 2 for theme in themes)

    issues = payload["issues"]
    assert len(issues) == 6
    assert all(int(issue["dedupe_cluster_id"]) >= 1 for issue in issues)
    assert all(int(issue["theme_cluster_id"]) >= 1 for issue in issues)

    markdown = out_md.read_text(encoding="utf-8")
    assert "Backlog Triage Report" in markdown
    assert "Common Across Groups" in markdown
