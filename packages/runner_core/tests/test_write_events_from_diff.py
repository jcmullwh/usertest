from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from normalized_events import iter_events_jsonl

from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once


def _make_dummy_codex_binary_that_edits(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_edit.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                "",
                "def main() -> int:",
                "    argv = sys.argv[1:]",
                "    cd: str | None = None",
                "    out_path: str | None = None",
                "    if '--cd' in argv:",
                "        idx = argv.index('--cd')",
                "        if idx + 1 < len(argv):",
                "            cd = argv[idx + 1]",
                "    if '--output-last-message' in argv:",
                "        idx = argv.index('--output-last-message')",
                "        if idx + 1 < len(argv):",
                "            out_path = argv[idx + 1]",
                "",
                "    if cd is not None:",
                "        os.chdir(cd)",
                "",
                "    # Simulate an edit in the workspace.",
                "    p = Path('README.md')",
                "    content = p.read_text(encoding='utf-8') + '\\nchanged\\n'",
                "    p.write_text(content, encoding='utf-8')",
                "",
                "    report = {",
                "        'schema_version': 1,",
                "        'persona': {'name': 'Evaluator'},",
                "        'mission': 'Assess fit quickly and safely.',",
                "        'minimal_mental_model': {",
                "            'summary': 'dummy',",
                "            'entry_points': ['README.md'],",
                "        },",
                "        'confidence_signals': {'found': ['ok'], 'missing': ['ok']},",
                "        'confusion_points': [],",
                "        'adoption_decision': {",
                "            'recommendation': 'investigate',",
                "            'rationale': 'ok',",
                "        },",
                "        'suggested_changes': [],",
                "    }",
                "    if out_path is not None:",
                "        Path(out_path).write_text(json.dumps(report) + '\\n', encoding='utf-8')",
                "",
                "    msg = {'id': '1', 'msg': {'type': 'agent_message', 'message': 'edited'}}",
                "    print(json.dumps(msg))",
                "    return 0",
                "",
                "",
                "if __name__ == '__main__':",
                "    raise SystemExit(main())",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    if os.name == "nt":
        wrapper = tmp_path / "dummy_codex_edit.cmd"
        wrapper.write_text(
            "\n".join(
                [
                    "@echo off",
                    f"\"{sys.executable}\" \"{script}\" %*",
                    "exit /b %ERRORLEVEL%",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return str(wrapper)

    wrapper = tmp_path / "dummy_codex_edit.sh"
    wrapper_text = f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n"
    wrapper.write_text(wrapper_text, encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_allow_edits_appends_write_events_from_diff(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    (target / "USERS.md").write_text("# Users\n", encoding="utf-8")

    dummy_binary = _make_dummy_codex_binary_that_edits(tmp_path)
    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
        ),
    )

    assert result.exit_code == 0
    assert not result.report_validation_errors

    diff_numstat = json.loads((result.run_dir / "diff_numstat.json").read_text(encoding="utf-8"))
    assert any(item.get("path") == "README.md" for item in diff_numstat)

    events = list(iter_events_jsonl(result.run_dir / "normalized_events.jsonl"))
    assert any(e.get("type") == "write_file" for e in events)

    metrics = json.loads((result.run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics.get("lines_added_total", 0) > 0
    assert "README.md" in metrics.get("distinct_files_written", [])
