from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_core.catalog import (
    CatalogConfig,
    CatalogError,
    MissionSpec,
    PersonaSpec,
    discover_missions,
    discover_personas,
)


@dataclass(frozen=True)
class EffectiveRunSpec:
    persona_id: str
    persona_name: str
    persona_md_resolved: str
    mission_id: str
    mission_name: str
    mission_md_resolved: str
    execution_mode: str
    prompt_template_path: Path
    report_schema_path: Path
    prompt_template_text: str
    report_schema_dict: dict[str, Any]


@dataclass(frozen=True)
class ResolvedRunInputs:
    effective: EffectiveRunSpec
    persona: PersonaSpec
    mission: MissionSpec


class RunSpecError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        normalized_code = (
            code.strip()
            if isinstance(code, str) and code.strip()
            else "invalid_run_spec"
        )
        normalized_details = dict(details) if isinstance(details, dict) else {}
        if not normalized_details:
            normalized_details = {"reason": message}
        normalized_hint = (
            hint.strip()
            if isinstance(hint, str) and hint.strip()
            else (
                "Validate persona/mission IDs and catalog file paths, then rerun "
                "`usertest personas list` and `usertest missions list`."
            )
        )
        self.code = normalized_code
        self.details = normalized_details
        self.hint = normalized_hint


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RunSpecError(
            f"Failed to read {path}: {e}",
            code="runspec_json_read_failed",
            details={"path": str(path), "error": str(e)},
            hint="Ensure the referenced JSON file exists and is readable.",
        ) from e
    except json.JSONDecodeError as e:
        raise RunSpecError(
            f"Failed to parse JSON in {path}: {e}",
            code="runspec_json_parse_failed",
            details={
                "path": str(path),
                "line": e.lineno,
                "column": e.colno,
                "error": e.msg,
            },
            hint="Fix JSON syntax in the referenced schema file.",
        ) from e
    if not isinstance(raw, dict):
        raise RunSpecError(
            f"Expected JSON object in {path}, got {type(raw).__name__}.",
            code="runspec_json_not_object",
            details={"path": str(path), "json_type": type(raw).__name__},
            hint="Use a JSON object (`{ ... }`) as the schema root.",
        )
    return raw


def _resolve_file_under_dir(*, base_dir: Path, rel: str, kind: str) -> Path:
    raw = Path(rel)
    candidate = raw if raw.is_absolute() else (base_dir / raw)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate
    if not resolved.exists():
        kind_code = "".join(ch if ch.isalnum() else "_" for ch in kind.lower()).strip("_")
        raise RunSpecError(
            f"Missing {kind} file: {resolved}",
            code=f"missing_{kind_code}_file" if kind_code else "missing_runspec_file",
            details={
                "kind": kind,
                "path": str(resolved),
                "base_dir": str(base_dir),
                "requested": rel,
            },
            hint=f"Fix the mission/catalog `{kind}` path so it points to an existing file.",
        )
    return resolved


def resolve_effective_run_spec(
    *,
    runner_repo_root: Path,
    target_repo_root: Path,
    catalog_config: CatalogConfig,
    persona_id: str | None,
    mission_id: str | None,
) -> EffectiveRunSpec:
    return resolve_effective_run_inputs(
        runner_repo_root=runner_repo_root,
        target_repo_root=target_repo_root,
        catalog_config=catalog_config,
        persona_id=persona_id,
        mission_id=mission_id,
    ).effective


def resolve_effective_run_inputs(
    *,
    runner_repo_root: Path,
    target_repo_root: Path,
    catalog_config: CatalogConfig,
    persona_id: str | None,
    mission_id: str | None,
) -> ResolvedRunInputs:
    try:
        personas = discover_personas(catalog_config)
        missions = discover_missions(catalog_config)
    except CatalogError as e:
        hint = (
            "Fix the catalog inputs so all persona/mission IDs are unique and valid "
            "frontmatter is present."
        )
        raise RunSpecError(
            str(e),
            code=getattr(e, "code", None),
            details=getattr(e, "details", None),
            hint=hint,
        ) from e

    resolved_persona_id = persona_id or catalog_config.defaults_persona_id
    if not resolved_persona_id:
        raise RunSpecError(
            "No persona_id provided and catalog has no defaults.persona_id. "
            "Specify --persona-id or set defaults.persona_id in catalog config.",
            code="missing_default_persona_id",
            details={"field": "defaults.persona_id"},
            hint="Set defaults.persona_id in configs/catalog.yaml or pass --persona-id.",
        )

    resolved_mission_id = mission_id or catalog_config.defaults_mission_id
    if not resolved_mission_id:
        raise RunSpecError(
            "No mission_id provided and catalog has no defaults.mission_id. "
            "Specify --mission-id or set defaults.mission_id in catalog config.",
            code="missing_default_mission_id",
            details={"field": "defaults.mission_id"},
            hint="Set defaults.mission_id in configs/catalog.yaml or pass --mission-id.",
        )

    persona: PersonaSpec | None = personas.get(resolved_persona_id)
    if persona is None:
        raise RunSpecError(
            f"Unknown persona id: {resolved_persona_id!r}",
            code="unknown_persona_id",
            details={"persona_id": resolved_persona_id},
            hint="Use `usertest personas list` or set defaults.persona_id in configs/catalog.yaml.",
        )

    mission: MissionSpec | None = missions.get(resolved_mission_id)
    if mission is None:
        raise RunSpecError(
            f"Unknown mission id: {resolved_mission_id!r}",
            code="unknown_mission_id",
            details={"mission_id": resolved_mission_id},
            hint="Use `usertest missions list` or set defaults.mission_id in configs/catalog.yaml.",
        )

    if mission.execution_mode != "single_pass_inline_report":
        raise RunSpecError(
            f"Unsupported execution_mode={mission.execution_mode!r} for mission {mission.id!r}.",
            code="unsupported_execution_mode",
            details={"mission_id": mission.id, "execution_mode": mission.execution_mode},
            hint="Update the mission frontmatter `execution_mode` or extend runner support.",
        )

    prompt_template_path = _resolve_file_under_dir(
        base_dir=catalog_config.prompt_templates_dir,
        rel=mission.prompt_template,
        kind="prompt template",
    )
    report_schema_path = _resolve_file_under_dir(
        base_dir=catalog_config.report_schemas_dir,
        rel=mission.report_schema,
        kind="report schema",
    )

    try:
        prompt_template_text = prompt_template_path.read_text(encoding="utf-8")
    except OSError as e:
        raise RunSpecError(
            f"Failed to read prompt template {prompt_template_path}: {e}",
            code="prompt_template_read_failed",
            details={"path": str(prompt_template_path), "error": str(e)},
            hint="Ensure mission.prompt_template points to a readable UTF-8 markdown file.",
        ) from e
    report_schema_dict = _load_json_object(report_schema_path)

    effective = EffectiveRunSpec(
        persona_id=persona.id,
        persona_name=persona.name,
        persona_md_resolved=persona.body_md,
        mission_id=mission.id,
        mission_name=mission.name,
        mission_md_resolved=mission.body_md,
        execution_mode=mission.execution_mode,
        prompt_template_path=prompt_template_path,
        report_schema_path=report_schema_path,
        prompt_template_text=prompt_template_text,
        report_schema_dict=report_schema_dict,
    )
    return ResolvedRunInputs(effective=effective, persona=persona, mission=mission)
