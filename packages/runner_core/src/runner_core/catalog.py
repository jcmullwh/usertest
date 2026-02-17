from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_CATALOG_VERSION = 1
_ALLOWED_EXECUTION_MODES: frozenset[str] = frozenset({"single_pass_inline_report"})


@dataclass(frozen=True)
class PersonaSpec:
    id: str
    name: str
    extends: str | None
    body_md: str
    source_path: Path


@dataclass(frozen=True)
class MissionSpec:
    id: str
    name: str
    extends: str | None
    tags: tuple[str, ...]
    execution_mode: str
    prompt_template: str
    report_schema: str
    body_md: str
    source_path: Path
    requires_shell: bool = False
    requires_edits: bool = False


@dataclass(frozen=True)
class CatalogConfig:
    version: int
    personas_dirs: tuple[Path, ...]
    missions_dirs: tuple[Path, ...]
    prompt_templates_dir: Path
    report_schemas_dir: Path
    defaults_persona_id: str | None
    defaults_mission_id: str | None


class CatalogError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise CatalogError(f"Failed to read {path}: {e}") from e
    except yaml.YAMLError as e:
        raise CatalogError(f"Failed to parse YAML in {path}: {e}") from e

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CatalogError(f"Expected a YAML mapping in {path}, got {type(raw).__name__}.")
    return raw


def _ensure_no_unknown_keys(*, data: dict[str, Any], allowed: set[str], path: Path) -> None:
    unknown = set(data) - allowed
    if not unknown:
        return
    if unknown == {"meta"}:
        meta = data.get("meta")
        if meta is None or isinstance(meta, dict):
            return
    unknown_list = ", ".join(sorted(unknown))
    allowed_list = ", ".join(sorted(allowed))
    raise CatalogError(f"Unknown keys in {path}: {unknown_list}. Allowed: {allowed_list}.")


def _parse_rel_path(value: Any, *, root: Path, path: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"Expected non-empty string for {field} in {path}.")
    raw = Path(value)
    candidate = raw if raw.is_absolute() else (root / raw)
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return candidate


def _parse_rel_path_list(value: Any, *, root: Path, path: Path, field: str) -> tuple[Path, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CatalogError(f"Expected list for {field} in {path}.")

    out: list[Path] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"Expected non-empty string for {field}[{idx}] in {path}.")
        raw = Path(item)
        candidate = raw if raw.is_absolute() else (root / raw)
        try:
            out.append(candidate.resolve(strict=False))
        except OSError:
            out.append(candidate)
    return tuple(out)


@dataclass(frozen=True)
class _CatalogOverride:
    version: int | None
    personas_dirs: tuple[Path, ...]
    missions_dirs: tuple[Path, ...]
    prompt_templates_dir: Path | None
    report_schemas_dir: Path | None
    defaults_persona_id: str | None
    defaults_mission_id: str | None


def _parse_catalog_override(*, data: dict[str, Any], root: Path, path: Path) -> _CatalogOverride:
    allowed = {
        "version",
        "personas_dirs",
        "missions_dirs",
        "prompt_templates_dir",
        "report_schemas_dir",
        "defaults",
        "meta",
    }
    _ensure_no_unknown_keys(data=data, allowed=allowed, path=path)

    version: int | None = None
    raw_version = data.get("version")
    if raw_version is not None:
        if not isinstance(raw_version, int):
            raise CatalogError(f"Expected integer version in {path}.")
        version = raw_version

    personas_dirs = _parse_rel_path_list(
        data.get("personas_dirs"), root=root, path=path, field="personas_dirs"
    )
    missions_dirs = _parse_rel_path_list(
        data.get("missions_dirs"), root=root, path=path, field="missions_dirs"
    )

    prompt_templates_dir: Path | None = None
    if data.get("prompt_templates_dir") is not None:
        prompt_templates_dir = _parse_rel_path(
            data.get("prompt_templates_dir"), root=root, path=path, field="prompt_templates_dir"
        )

    report_schemas_dir: Path | None = None
    if data.get("report_schemas_dir") is not None:
        report_schemas_dir = _parse_rel_path(
            data.get("report_schemas_dir"), root=root, path=path, field="report_schemas_dir"
        )

    defaults_persona_id: str | None = None
    defaults_mission_id: str | None = None

    defaults = data.get("defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            raise CatalogError(f"Expected mapping for defaults in {path}.")
        _ensure_no_unknown_keys(
            data=defaults,
            allowed={"persona_id", "mission_id", "meta"},
            path=path,
        )

        raw_persona_id = defaults.get("persona_id")
        if raw_persona_id is not None:
            if not isinstance(raw_persona_id, str) or not raw_persona_id.strip():
                raise CatalogError(f"defaults.persona_id must be a non-empty string in {path}.")
            defaults_persona_id = raw_persona_id.strip()

        raw_mission_id = defaults.get("mission_id")
        if raw_mission_id is not None:
            if not isinstance(raw_mission_id, str) or not raw_mission_id.strip():
                raise CatalogError(f"defaults.mission_id must be a non-empty string in {path}.")
            defaults_mission_id = raw_mission_id.strip()

    return _CatalogOverride(
        version=version,
        personas_dirs=personas_dirs,
        missions_dirs=missions_dirs,
        prompt_templates_dir=prompt_templates_dir,
        report_schemas_dir=report_schemas_dir,
        defaults_persona_id=defaults_persona_id,
        defaults_mission_id=defaults_mission_id,
    )


def load_catalog_config(repo_root: Path, target_repo_root: Path | None) -> CatalogConfig:
    base_path = repo_root / "configs" / "catalog.yaml"
    base_raw = _load_yaml_mapping(base_path)
    base_override = _parse_catalog_override(data=base_raw, root=repo_root, path=base_path)

    if base_override.version is None:
        raise CatalogError(f"Missing required version in {base_path}.")
    if base_override.version != _CATALOG_VERSION:
        raise CatalogError(f"Unsupported catalog version {base_override.version} in {base_path}.")

    if base_override.prompt_templates_dir is None:
        raise CatalogError(f"Missing required prompt_templates_dir in {base_path}.")
    if base_override.report_schemas_dir is None:
        raise CatalogError(f"Missing required report_schemas_dir in {base_path}.")

    base_defaults_persona_id = base_override.defaults_persona_id
    base_defaults_mission_id = base_override.defaults_mission_id

    merged = CatalogConfig(
        version=base_override.version,
        personas_dirs=base_override.personas_dirs,
        missions_dirs=base_override.missions_dirs,
        prompt_templates_dir=base_override.prompt_templates_dir,
        report_schemas_dir=base_override.report_schemas_dir,
        defaults_persona_id=base_defaults_persona_id,
        defaults_mission_id=base_defaults_mission_id,
    )

    if target_repo_root is None:
        return merged

    target_path = target_repo_root / ".usertest" / "catalog.yaml"
    if not target_path.exists():
        return merged

    target_raw = _load_yaml_mapping(target_path)
    target_override = _parse_catalog_override(
        data=target_raw, root=target_repo_root, path=target_path
    )

    if target_override.version is not None and target_override.version != merged.version:
        raise CatalogError(
            f"Catalog version mismatch: base={merged.version} ({base_path}), "
            f"target={target_override.version} ({target_path})."
        )

    return CatalogConfig(
        version=merged.version,
        personas_dirs=(*merged.personas_dirs, *target_override.personas_dirs),
        missions_dirs=(*merged.missions_dirs, *target_override.missions_dirs),
        prompt_templates_dir=target_override.prompt_templates_dir or merged.prompt_templates_dir,
        report_schemas_dir=target_override.report_schemas_dir or merged.report_schemas_dir,
        defaults_persona_id=(
            target_override.defaults_persona_id
            if target_override.defaults_persona_id is not None
            else merged.defaults_persona_id
        ),
        defaults_mission_id=(
            target_override.defaults_mission_id
            if target_override.defaults_mission_id is not None
            else merged.defaults_mission_id
        ),
    )


def _parse_frontmatter(*, text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise CatalogError(f"Missing YAML frontmatter in {path} (expected leading '---').")

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise CatalogError(f"Invalid YAML frontmatter start in {path} (expected '---').")

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise CatalogError(f"Unterminated YAML frontmatter in {path} (missing closing '---').")

    fm_text = "\n".join(lines[1:end_idx]).strip()
    body_text = "\n".join(lines[end_idx + 1 :]).strip()

    try:
        fm_raw = yaml.safe_load(fm_text) if fm_text else {}
    except yaml.YAMLError as e:
        raise CatalogError(f"Failed to parse YAML frontmatter in {path}: {e}") from e

    if fm_raw is None:
        fm_raw = {}
    if not isinstance(fm_raw, dict):
        raise CatalogError(f"Expected YAML frontmatter mapping in {path}.")

    return fm_raw, body_text


def _require_nonempty_str(value: Any, *, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"Missing or invalid {field} in {path}.")
    return value.strip()


def _parse_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_tags(value: Any, *, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CatalogError(f"tags must be a list in {path}.")
    tags: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"tags[{idx}] must be a non-empty string in {path}.")
        tags.append(item.strip())
    return tuple(tags)


def _merge_tags(base: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in [*base, *extra]:
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return tuple(out)


def discover_personas(config: CatalogConfig) -> dict[str, PersonaSpec]:
    raw_by_id: dict[str, PersonaSpec] = {}

    for dir_path in config.personas_dirs:
        if not dir_path.exists() or not dir_path.is_dir():
            raise CatalogError(f"Persona directory not found: {dir_path}")

        for doc_path in sorted(dir_path.rglob("*.persona.md"), key=lambda p: str(p)):
            text = doc_path.read_text(encoding="utf-8")
            fm, body = _parse_frontmatter(text=text, path=doc_path)

            persona_id = _require_nonempty_str(fm.get("id"), path=doc_path, field="id")
            name = _require_nonempty_str(fm.get("name"), path=doc_path, field="name")
            extends = _parse_optional_str(fm.get("extends"))

            spec = PersonaSpec(
                id=persona_id,
                name=name,
                extends=extends,
                body_md=body,
                source_path=doc_path,
            )

            if persona_id in raw_by_id:
                prev = raw_by_id[persona_id].source_path
                raise CatalogError(
                    f"Duplicate persona id {persona_id!r} in {prev} and {doc_path}.",
                    code="duplicate_persona_id",
                    details={"id": persona_id, "paths": [str(prev), str(doc_path)]},
                )
            raw_by_id[persona_id] = spec

    return resolve_persona_extends(raw_by_id)


def discover_missions(config: CatalogConfig) -> dict[str, MissionSpec]:
    raw_by_id: dict[str, MissionSpec] = {}

    for dir_path in config.missions_dirs:
        if not dir_path.exists() or not dir_path.is_dir():
            raise CatalogError(f"Mission directory not found: {dir_path}")

        for doc_path in sorted(dir_path.rglob("*.mission.md"), key=lambda p: str(p)):
            text = doc_path.read_text(encoding="utf-8")
            fm, body = _parse_frontmatter(text=text, path=doc_path)

            mission_id = _require_nonempty_str(fm.get("id"), path=doc_path, field="id")
            name = _require_nonempty_str(fm.get("name"), path=doc_path, field="name")
            extends = _parse_optional_str(fm.get("extends"))
            tags = _parse_tags(fm.get("tags"), path=doc_path)

            execution_mode = _parse_optional_str(fm.get("execution_mode")) or ""
            if execution_mode and execution_mode not in _ALLOWED_EXECUTION_MODES:
                raise CatalogError(
                    f"Unsupported execution_mode {execution_mode!r} in {doc_path}. "
                    f"Allowed: {', '.join(sorted(_ALLOWED_EXECUTION_MODES))}."
                )

            prompt_template = _parse_optional_str(fm.get("prompt_template")) or ""
            report_schema = _parse_optional_str(fm.get("report_schema")) or ""

            requires_shell_raw = fm.get("requires_shell")
            if requires_shell_raw is None:
                requires_shell = False
            elif isinstance(requires_shell_raw, bool):
                requires_shell = requires_shell_raw
            else:
                raise CatalogError(
                    "Expected boolean requires_shell in "
                    f"{doc_path}, got {type(requires_shell_raw).__name__}."
                )

            requires_edits_raw = fm.get("requires_edits")
            if requires_edits_raw is None:
                requires_edits = False
            elif isinstance(requires_edits_raw, bool):
                requires_edits = requires_edits_raw
            else:
                raise CatalogError(
                    "Expected boolean requires_edits in "
                    f"{doc_path}, got {type(requires_edits_raw).__name__}."
                )

            spec = MissionSpec(
                id=mission_id,
                name=name,
                extends=extends,
                tags=tags,
                execution_mode=execution_mode,
                prompt_template=prompt_template,
                report_schema=report_schema,
                body_md=body,
                source_path=doc_path,
                requires_shell=requires_shell,
                requires_edits=requires_edits,
            )

            if mission_id in raw_by_id:
                prev = raw_by_id[mission_id].source_path
                raise CatalogError(
                    f"Duplicate mission id {mission_id!r} in {prev} and {doc_path}.",
                    code="duplicate_mission_id",
                    details={"id": mission_id, "paths": [str(prev), str(doc_path)]},
                )
            raw_by_id[mission_id] = spec

    return resolve_mission_extends(raw_by_id)


def resolve_persona_extends(personas: dict[str, PersonaSpec]) -> dict[str, PersonaSpec]:
    resolved: dict[str, PersonaSpec] = {}
    visiting: set[str] = set()

    def _resolve_one(persona_id: str) -> PersonaSpec:
        if persona_id in resolved:
            return resolved[persona_id]
        if persona_id in visiting:
            raise CatalogError(f"Persona extends cycle detected at {persona_id!r}.")

        spec = personas.get(persona_id)
        if spec is None:
            raise CatalogError(f"Unknown persona id referenced by extends: {persona_id!r}.")

        visiting.add(persona_id)
        parts: list[str] = []
        if spec.extends:
            parts.append(_resolve_one(spec.extends).body_md)
        if spec.body_md.strip():
            parts.append(spec.body_md.strip())
        body = "\n\n".join(parts).strip()

        visiting.remove(persona_id)

        resolved_spec = PersonaSpec(
            id=spec.id,
            name=spec.name,
            extends=spec.extends,
            body_md=body,
            source_path=spec.source_path,
        )
        resolved[persona_id] = resolved_spec
        return resolved_spec

    for pid in list(personas.keys()):
        _resolve_one(pid)

    return resolved


def resolve_mission_extends(missions: dict[str, MissionSpec]) -> dict[str, MissionSpec]:
    resolved: dict[str, MissionSpec] = {}
    visiting: set[str] = set()

    def _resolve_one(mission_id: str) -> MissionSpec:
        if mission_id in resolved:
            return resolved[mission_id]
        if mission_id in visiting:
            raise CatalogError(f"Mission extends cycle detected at {mission_id!r}.")

        spec = missions.get(mission_id)
        if spec is None:
            raise CatalogError(f"Unknown mission id referenced by extends: {mission_id!r}.")

        visiting.add(mission_id)

        base: MissionSpec | None = None
        if spec.extends:
            base = _resolve_one(spec.extends)

        execution_mode = spec.execution_mode or (base.execution_mode if base else "")
        prompt_template = spec.prompt_template or (base.prompt_template if base else "")
        report_schema = spec.report_schema or (base.report_schema if base else "")
        requires_shell = bool(spec.requires_shell or (base.requires_shell if base else False))
        requires_edits = bool(spec.requires_edits or (base.requires_edits if base else False))

        if execution_mode and execution_mode not in _ALLOWED_EXECUTION_MODES:
            raise CatalogError(
                f"Unsupported execution_mode {execution_mode!r} in resolved mission {mission_id!r}."
            )
        if not execution_mode:
            raise CatalogError(
                f"Missing execution_mode in mission {mission_id!r} ({spec.source_path})."
            )
        if not prompt_template:
            raise CatalogError(
                f"Missing prompt_template in mission {mission_id!r} ({spec.source_path})."
            )
        if not report_schema:
            raise CatalogError(
                f"Missing report_schema in mission {mission_id!r} ({spec.source_path})."
            )

        body_parts: list[str] = []
        if base is not None and base.body_md.strip():
            body_parts.append(base.body_md.strip())
        if spec.body_md.strip():
            body_parts.append(spec.body_md.strip())
        body = "\n\n".join(body_parts).strip()

        tags = _merge_tags(base.tags if base else (), spec.tags)

        visiting.remove(mission_id)

        resolved_spec = MissionSpec(
            id=spec.id,
            name=spec.name,
            extends=spec.extends,
            tags=tags,
            execution_mode=execution_mode,
            prompt_template=prompt_template,
            report_schema=report_schema,
            body_md=body,
            source_path=spec.source_path,
            requires_shell=requires_shell,
            requires_edits=requires_edits,
        )
        resolved[mission_id] = resolved_spec
        return resolved_spec

    for mid in list(missions.keys()):
        _resolve_one(mid)

    return resolved
