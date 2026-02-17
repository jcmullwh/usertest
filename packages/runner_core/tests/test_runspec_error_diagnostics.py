from __future__ import annotations

import json
from pathlib import Path

from runner_core import RunnerConfig, RunRequest, run_once


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_runner_config(runner_root: Path, tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )


def _make_target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "README.md", "# hi\n")
    _write(target / "USERS.md", "# Users\n")
    return target


def test_run_once_writes_structured_runspec_error_json(tmp_path: Path) -> None:
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
                "  persona_id: dup",
                "  mission_id: m",
                "",
            ]
        ),
    )
    _write(
        runner_root / "configs" / "personas" / "a.persona.md",
        "\n".join(["---", "id: dup", "name: A", "extends: null", "---", "A", ""]),
    )
    _write(
        runner_root / "configs" / "personas" / "b.persona.md",
        "\n".join(["---", "id: dup", "name: B", "extends: null", "---", "B", ""]),
    )
    _write(
        runner_root / "configs" / "missions" / "m.mission.md",
        "\n".join(
            [
                "---",
                "id: m",
                "name: M",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: t.prompt.md",
                "report_schema: s.schema.json",
                "---",
                "Mission",
                "",
            ]
        ),
    )
    _write(runner_root / "configs" / "prompt_templates" / "t.prompt.md", "x\n")
    _write(runner_root / "configs" / "report_schemas" / "s.schema.json", "{\"type\":\"object\"}\n")

    target = _make_target(tmp_path)

    cfg = _make_runner_config(runner_root, tmp_path)

    result = run_once(cfg, RunRequest(repo=str(target), agent="codex", policy="safe"))
    assert result.exit_code == 1

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj["type"] == "RunSpecError"
    assert error_obj.get("code") == "duplicate_persona_id"
    assert isinstance(error_obj.get("details"), dict)
    assert error_obj["details"].get("id") == "dup"
    assert isinstance(error_obj["details"].get("paths"), list)
    assert error_obj.get("hint")
    assert any(line.startswith("code=") for line in result.report_validation_errors)
    assert any(line.startswith("details=") for line in result.report_validation_errors)
    assert any(line.startswith("hint=") for line in result.report_validation_errors)


def test_run_once_writes_structured_runspec_error_for_missing_prompt_template(
    tmp_path: Path,
) -> None:
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
        "\n".join(["---", "id: p", "name: P", "extends: null", "---", "P", ""]),
    )
    _write(
        runner_root / "configs" / "missions" / "m.mission.md",
        "\n".join(
            [
                "---",
                "id: m",
                "name: M",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: missing.prompt.md",
                "report_schema: s.schema.json",
                "---",
                "Mission",
                "",
            ]
        ),
    )
    _write(runner_root / "configs" / "report_schemas" / "s.schema.json", "{\"type\":\"object\"}\n")

    target = _make_target(tmp_path)
    cfg = _make_runner_config(runner_root, tmp_path)
    result = run_once(cfg, RunRequest(repo=str(target), agent="codex", policy="safe"))
    assert result.exit_code == 1

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj["type"] == "RunSpecError"
    assert error_obj.get("code") == "missing_prompt_template_file"
    assert isinstance(error_obj.get("details"), dict)
    assert error_obj["details"].get("requested") == "missing.prompt.md"
    assert error_obj.get("hint")


def test_run_once_writes_structured_runspec_error_for_invalid_schema_json(tmp_path: Path) -> None:
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
        "\n".join(["---", "id: p", "name: P", "extends: null", "---", "P", ""]),
    )
    _write(
        runner_root / "configs" / "missions" / "m.mission.md",
        "\n".join(
            [
                "---",
                "id: m",
                "name: M",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: t.prompt.md",
                "report_schema: s.schema.json",
                "---",
                "Mission",
                "",
            ]
        ),
    )
    _write(runner_root / "configs" / "prompt_templates" / "t.prompt.md", "x\n")
    _write(runner_root / "configs" / "report_schemas" / "s.schema.json", "{not_json}\n")

    target = _make_target(tmp_path)
    cfg = _make_runner_config(runner_root, tmp_path)
    result = run_once(cfg, RunRequest(repo=str(target), agent="codex", policy="safe"))
    assert result.exit_code == 1

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj["type"] == "RunSpecError"
    assert error_obj.get("code") == "runspec_json_parse_failed"
    assert isinstance(error_obj.get("details"), dict)
    assert error_obj["details"].get("path", "").endswith("s.schema.json")
    assert error_obj.get("hint")
