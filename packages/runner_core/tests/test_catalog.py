from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.catalog import CatalogConfig, CatalogError, discover_missions, discover_personas
from runner_core.execution_backend import ExecutionBackendContext


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


def test_discover_personas_errors_on_duplicate_ids(tmp_path: Path) -> None:
    d1 = tmp_path / "p1"
    d2 = tmp_path / "p2"
    _write(d1 / "a.persona.md", _persona_doc(persona_id="dup", name="A", extends=None, body="A"))
    _write(d2 / "b.persona.md", _persona_doc(persona_id="dup", name="B", extends=None, body="B"))

    cfg = CatalogConfig(
        version=1,
        personas_dirs=(d1, d2),
        missions_dirs=(),
        prompt_templates_dir=tmp_path / "templates",
        report_schemas_dir=tmp_path / "schemas",
        defaults_persona_id=None,
        defaults_mission_id=None,
    )

    with pytest.raises(CatalogError) as exc:
        discover_personas(cfg)
    msg = str(exc.value)
    assert "Duplicate persona id" in msg
    assert "dup" in msg
    assert exc.value.code == "duplicate_persona_id"
    assert exc.value.details.get("id") == "dup"
    paths = exc.value.details.get("paths")
    assert isinstance(paths, list)
    assert any("a.persona.md" in str(p) for p in paths)
    assert any("b.persona.md" in str(p) for p in paths)


def test_discover_missions_errors_on_duplicate_ids(tmp_path: Path) -> None:
    d1 = tmp_path / "m1"
    d2 = tmp_path / "m2"
    _write(
        d1 / "a.mission.md",
        _mission_doc(
            mission_id="dup",
            name="A",
            extends=None,
            execution_mode="single_pass_inline_report",
            prompt_template="t.prompt.md",
            report_schema="s.schema.json",
            body="A",
        ),
    )
    _write(
        d2 / "b.mission.md",
        _mission_doc(
            mission_id="dup",
            name="B",
            extends=None,
            execution_mode="single_pass_inline_report",
            prompt_template="t.prompt.md",
            report_schema="s.schema.json",
            body="B",
        ),
    )

    cfg = CatalogConfig(
        version=1,
        personas_dirs=(),
        missions_dirs=(d1, d2),
        prompt_templates_dir=tmp_path / "templates",
        report_schemas_dir=tmp_path / "schemas",
        defaults_persona_id=None,
        defaults_mission_id=None,
    )

    with pytest.raises(CatalogError) as exc:
        discover_missions(cfg)
    msg = str(exc.value)
    assert "Duplicate mission id" in msg
    assert "dup" in msg
    assert exc.value.code == "duplicate_mission_id"
    assert exc.value.details.get("id") == "dup"
    paths = exc.value.details.get("paths")
    assert isinstance(paths, list)
    assert any("a.mission.md" in str(p) for p in paths)
    assert any("b.mission.md" in str(p) for p in paths)


def test_extends_resolves_for_personas_and_missions(tmp_path: Path) -> None:
    personas_dir = tmp_path / "personas"
    missions_dir = tmp_path / "missions"
    templates_dir = tmp_path / "templates"
    schemas_dir = tmp_path / "schemas"
    templates_dir.mkdir(parents=True)
    schemas_dir.mkdir(parents=True)

    _write(
        personas_dir / "base.persona.md",
        _persona_doc(persona_id="base", name="Base", extends=None, body="Base"),
    )
    _write(
        personas_dir / "child.persona.md",
        _persona_doc(persona_id="child", name="Child", extends="base", body="Child"),
    )

    _write(
        missions_dir / "base.mission.md",
        _mission_doc(
            mission_id="base",
            name="Base",
            extends=None,
            execution_mode="single_pass_inline_report",
            prompt_template="t.prompt.md",
            report_schema="s.schema.json",
            body="Base mission",
        ),
    )
    # Inherit metadata via extends by omitting those fields.
    _write(
        missions_dir / "child.mission.md",
        _mission_doc(
            mission_id="child",
            name="Child",
            extends="base",
            execution_mode=None,
            prompt_template=None,
            report_schema=None,
            body="Child mission",
        ),
    )

    cfg = CatalogConfig(
        version=1,
        personas_dirs=(personas_dir,),
        missions_dirs=(missions_dir,),
        prompt_templates_dir=templates_dir,
        report_schemas_dir=schemas_dir,
        defaults_persona_id=None,
        defaults_mission_id=None,
    )

    personas = discover_personas(cfg)
    missions = discover_missions(cfg)

    assert "Base" in personas["child"].body_md
    assert "Child" in personas["child"].body_md
    assert "Base mission" in missions["child"].body_md
    assert "Child mission" in missions["child"].body_md
    assert missions["child"].prompt_template == "t.prompt.md"
    assert missions["child"].report_schema == "s.schema.json"


def test_discover_missions_errors_on_unsupported_execution_mode(tmp_path: Path) -> None:
    missions_dir = tmp_path / "missions"
    _write(
        missions_dir / "x.mission.md",
        _mission_doc(
            mission_id="x",
            name="X",
            extends=None,
            execution_mode="two_pass_blackbox",
            prompt_template="t.prompt.md",
            report_schema="s.schema.json",
            body="X",
        ),
    )

    cfg = CatalogConfig(
        version=1,
        personas_dirs=(),
        missions_dirs=(missions_dir,),
        prompt_templates_dir=tmp_path / "templates",
        report_schemas_dir=tmp_path / "schemas",
        defaults_persona_id=None,
        defaults_mission_id=None,
    )

    with pytest.raises(CatalogError) as exc:
        discover_missions(cfg)
    assert "Unsupported execution_mode" in str(exc.value)


def test_discover_missions_parses_requirements_flags(tmp_path: Path) -> None:
    missions_dir = tmp_path / "missions"
    _write(
        missions_dir / "m.mission.md",
        "\n".join(
            [
                "---",
                "id: m",
                "name: M",
                "extends: null",
                "tags: [test]",
                "requires_shell: true",
                "requires_edits: true",
                "execution_mode: single_pass_inline_report",
                "prompt_template: t.prompt.md",
                "report_schema: s.schema.json",
                "---",
                "Body",
                "",
            ]
        ),
    )

    cfg = CatalogConfig(
        version=1,
        personas_dirs=(),
        missions_dirs=(missions_dir,),
        prompt_templates_dir=tmp_path / "templates",
        report_schemas_dir=tmp_path / "schemas",
        defaults_persona_id=None,
        defaults_mission_id=None,
    )

    missions = discover_missions(cfg)
    spec = missions["m"]
    assert spec.requires_shell is True
    assert spec.requires_edits is True


def _make_dummy_codex_binary_with_report(tmp_path: Path, report: dict[str, object]) -> str:
    script = tmp_path / "dummy_codex_report.py"
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
                "    out_path: str | None = None",
                "    if '--output-last-message' in argv:",
                "        idx = argv.index('--output-last-message')",
                "        if idx + 1 < len(argv):",
                "            out_path = argv[idx + 1]",
                "",
                f"    report = {json.dumps(report, ensure_ascii=False)}",
                "",
                "    if out_path is not None:",
                "        Path(out_path).write_text(json.dumps(report) + '\\n', encoding='utf-8')",
                "",
                "    msg = {'id': '1', 'msg': {'type': 'agent_message', 'message': 'hi'}}",
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
        wrapper = tmp_path / "dummy_codex_report.cmd"
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

    wrapper = tmp_path / "dummy_codex_report.sh"
    wrapper.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n", encoding="utf-8"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_run_once_validates_against_mission_selected_schema(tmp_path: Path) -> None:
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
                "  mission_id: alpha",
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

    schema_alpha = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["alpha"],
        "properties": {"alpha": {"type": "string"}},
    }
    schema_beta = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["beta"],
        "properties": {"beta": {"type": "string"}},
    }
    _write(
        runner_root / "configs" / "report_schemas" / "alpha.schema.json",
        json.dumps(schema_alpha, indent=2, ensure_ascii=False) + "\n",
    )
    _write(
        runner_root / "configs" / "report_schemas" / "beta.schema.json",
        json.dumps(schema_beta, indent=2, ensure_ascii=False) + "\n",
    )

    _write(
        runner_root / "configs" / "missions" / "alpha.mission.md",
        _mission_doc(
            mission_id="alpha",
            name="Alpha",
            extends=None,
            execution_mode="single_pass_inline_report",
            prompt_template="t.prompt.md",
            report_schema="alpha.schema.json",
            body="Alpha body",
        ),
    )
    _write(
        runner_root / "configs" / "missions" / "beta.mission.md",
        _mission_doc(
            mission_id="beta",
            name="Beta",
            extends=None,
            execution_mode="single_pass_inline_report",
            prompt_template="t.prompt.md",
            report_schema="beta.schema.json",
            body="Beta body",
        ),
    )

    target = tmp_path / "target"
    target.mkdir()
    _write(target / "README.md", "# hi\n")
    _write(target / "USERS.md", "# Users\n")

    dummy_binary = _make_dummy_codex_binary_with_report(tmp_path, {"alpha": "ok"})
    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    ok_result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            persona_id="p",
            mission_id="alpha",
            seed=0,
        ),
    )
    assert ok_result.exit_code == 0
    assert not ok_result.report_validation_errors
    assert (ok_result.run_dir / "report.schema.json").exists()
    schema_used = json.loads((ok_result.run_dir / "report.schema.json").read_text(encoding="utf-8"))
    assert "alpha" in schema_used.get("required", [])

    bad_result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            persona_id="p",
            mission_id="beta",
            seed=1,
        ),
    )
    assert bad_result.exit_code == 0
    assert bad_result.report_validation_errors
    assert any("beta" in e for e in bad_result.report_validation_errors)


def test_run_once_docker_mount_keeps_posix_workspace_path_in_prompt(tmp_path: Path) -> None:
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
        "ENV\n${environment_json}\n",
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
            body="M body",
        ),
    )

    target = tmp_path / "target"
    target.mkdir()
    _write(target / "README.md", "# hi\n")
    _write(target / "USERS.md", "# Users\n")

    dummy_binary = _make_dummy_codex_binary_with_report(tmp_path, {"ok": "ok"})
    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    with patch(
        "runner_core.runner.prepare_execution_backend",
        return_value=ExecutionBackendContext(
            sandbox_instance=None,
            command_prefix=[],
            workspace_mount="/workspace",
            run_dir_mount=None,
        ),
    ):
        result = run_once(
            cfg,
            RunRequest(
                repo=str(target),
                agent="codex",
                policy="safe",
                persona_id="p",
                mission_id="m",
                seed=0,
                exec_backend="docker",
            ),
        )

    prompt_text = (result.run_dir / "prompt.txt").read_text(encoding="utf-8")
    assert '"path": "/workspace"' in prompt_text
