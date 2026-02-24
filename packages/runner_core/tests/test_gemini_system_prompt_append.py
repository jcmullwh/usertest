from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from runner_core import RunnerConfig, RunRequest, run_once


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _persona_doc(*, persona_id: str, name: str, extends: str | None, body: str) -> str:
    extends_value = "null" if extends is None else extends
    return "\n".join(
        [
            "---",
            f"id: {persona_id}",
            f"name: {name}",
            f"extends: {extends_value}",
            "---",
            body,
            "",
        ]
    )


def _mission_doc(
    *,
    mission_id: str,
    name: str,
    extends: str | None,
    execution_mode: str | None,
    prompt_template: str | None,
    report_schema: str | None,
    body: str,
) -> str:
    fm_lines = [
        "---",
        f"id: {mission_id}",
        f"name: {name}",
        f"extends: {'null' if extends is None else extends}",
        "tags: [test]",
    ]
    if execution_mode is not None:
        fm_lines.append(f"execution_mode: {execution_mode}")
    if prompt_template is not None:
        fm_lines.append(f"prompt_template: {prompt_template}")
    if report_schema is not None:
        fm_lines.append(f"report_schema: {report_schema}")
    fm_lines.append("---")
    return "\n".join([*fm_lines, body, ""])


def _make_dummy_gemini_binary(tmp_path: Path, *, expected_system_prompt: str) -> str:
    script = tmp_path / "dummy_gemini.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "import sys",
                "from pathlib import Path",
                "",
                "",
                "def main() -> int:",
                "    argv = sys.argv[1:]",
                "    if '--agent-system-prompt-file' not in argv:",
                "        print('missing --agent-system-prompt-file', file=sys.stderr)",
                "        return 2",
                "    idx = argv.index('--agent-system-prompt-file')",
                "    if idx + 1 >= len(argv):",
                "        print('missing system prompt path', file=sys.stderr)",
                "        return 2",
                "    system_path = Path(argv[idx + 1])",
                "    try:",
                "        payload = system_path.read_text(encoding='utf-8')",
                "    except OSError as e:",
                "        print(f'failed to read system prompt: {e}', file=sys.stderr)",
                "        return 3",
                f"    expected = {json.dumps(expected_system_prompt, ensure_ascii=False)}",
                "    if payload != expected:",
                "        print('unexpected system prompt payload', file=sys.stderr)",
                "        print(payload, file=sys.stderr)",
                "        return 4",
                "    print(json.dumps({'response': json.dumps({'ok': 'ok'})}))",
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
        wrapper = tmp_path / "dummy_gemini.cmd"
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

    wrapper = tmp_path / "dummy_gemini.sh"
    wrapper.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_gemini_system_prompt_append_is_composed_into_replacement_file(tmp_path: Path) -> None:
    runner_root = tmp_path / "runner_root"
    _write(
        runner_root / "configs" / "catalog.yaml",
        "\n".join(
            [
                "version: 1",
                "personas_dirs:",
                "  - configs/personas",
                "missions_dirs:",
                "  - configs/missions",
                "prompt_templates_dir: configs/prompt_templates",
                "report_schemas_dir: configs/report_schemas",
                "defaults:",
                "  persona_id: p",
                "  mission_id: m",
                "",
            ]
        ),
    )
    _write(
        runner_root / "configs" / "personas" / "p.persona.md",
        _persona_doc(persona_id="p", name="P", extends=None, body="P body"),
    )
    _write(
        runner_root / "configs" / "prompt_templates" / "t.prompt.md",
        "TEMPLATE\n${report_schema_json}\n",
    )
    _write(
        runner_root / "configs" / "report_schemas" / "s.schema.json",
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
    _write(
        runner_root / "configs" / "missions" / "m.mission.md",
        _mission_doc(
            mission_id="m",
            name="M",
            extends=None,
            execution_mode="single_pass_inline_report",
            prompt_template="t.prompt.md",
            report_schema="s.schema.json",
            body="Mission body",
        ),
    )

    target = tmp_path / "target"
    target.mkdir()
    _write(target / "README.md", "# hi\n")

    base_system_prompt = tmp_path / "base_system.md"
    base_system_prompt.write_text("BASE\n", encoding="utf-8")
    append_text = "APPEND"
    expected_merged = "BASE\n\nAPPEND\n"

    dummy_binary = _make_dummy_gemini_binary(tmp_path, expected_system_prompt=expected_merged)
    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"gemini": {"binary": dummy_binary, "output_format": "json"}},
        policies={"safe": {"gemini": {"sandbox": False, "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="gemini",
            policy="safe",
            persona_id="p",
            mission_id="m",
            agent_system_prompt_file=base_system_prompt,
            agent_append_system_prompt=append_text,
        ),
    )
    assert result.exit_code == 0
    assert not result.report_validation_errors
