# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.shared import *


def _default_settings_path(repo_root: Path) -> Path:
    return repo_root / "configs" / _SETTINGS_FILENAME


def _resolve_settings_path(*, repo_root: Path, value: Path | None) -> Path | None:
    if value is None:
        candidate = _default_settings_path(repo_root)
        return candidate if candidate.exists() else None
    if value.is_absolute():
        return value.resolve()
    return (repo_root / value).resolve()


def _load_cli_settings_doc(path: Path) -> dict[str, Any]:
    doc = _load_yaml(path)
    version = doc.get("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported settings version in {path}: {version!r}")
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError(f"Expected non-empty profiles mapping in {path}")
    for profile_name, profile_raw in profiles.items():
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise ValueError(f"Settings profile names must be non-empty strings in {path}")
        if not isinstance(profile_raw, dict):
            raise ValueError(f"Profile {profile_name!r} must be a mapping in {path}")
        unknown_sections = set(profile_raw.keys()) - _SETTINGS_ALLOWED_SECTIONS
        if unknown_sections:
            raise ValueError(
                f"Profile {profile_name!r} has unknown sections {sorted(unknown_sections)!r} in {path}"
            )
        for section_name, section_raw in profile_raw.items():
            if not isinstance(section_raw, dict):
                raise ValueError(
                    f"Profile {profile_name!r} section {section_name!r} must be a mapping in {path}"
                )
    default_profile = doc.get("default_profile")
    if default_profile is not None:
        if not isinstance(default_profile, str) or not default_profile.strip():
            raise ValueError(f"default_profile must be a non-empty string in {path}")
        if default_profile not in profiles:
            raise ValueError(
                f"default_profile {default_profile!r} was not found in profiles for {path}"
            )
    return doc


def _coerce_settings_bool(*, key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOLEAN_TRUE_VALUES:
            return True
        if lowered in _BOOLEAN_FALSE_VALUES:
            return False
    raise ValueError(f"Setting {key!r} must be a boolean value")


def _coerce_settings_value(
    *,
    key: str,
    value: Any,
    spec: _SettingsValueSpec,
    settings_path: Path,
    repo_root: Path,
) -> Any:
    if value is None:
        if spec.allow_none:
            return None
        raise ValueError(f"Setting {key!r} may not be null")

    if spec.kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"Setting {key!r} must be a string")
        return value

    if spec.kind == "choice":
        if not isinstance(value, str):
            raise ValueError(f"Setting {key!r} must be a string")
        normalized = value.strip()
        if normalized not in spec.choices:
            raise ValueError(
                f"Setting {key!r} must be one of {sorted(spec.choices)!r}; got {normalized!r}"
            )
        return normalized

    if spec.kind == "bool":
        return _coerce_settings_bool(key=key, value=value)

    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Setting {key!r} must be an integer")
        return value

    if spec.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Setting {key!r} must be a number")
        return float(value)

    if spec.kind == "path":
        if not isinstance(value, str):
            raise ValueError(f"Setting {key!r} must be a filesystem path string")
        path = Path(value)
        if not path.is_absolute():
            path = (repo_root / path).resolve()
        else:
            path = path.resolve()
        return path

    if spec.kind == "str_list":
        if not isinstance(value, list):
            raise ValueError(f"Setting {key!r} must be a list of strings")
        out: list[str] = []
        for idx, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"Setting {key!r}[{idx}] must be a string")
            out.append(item)
        return out

    raise ValueError(f"Unsupported settings spec kind for {key!r}: {spec.kind!r}")


def _settings_specs_for_args(args: argparse.Namespace) -> dict[str, _SettingsValueSpec]:
    specs = dict(_SETTINGS_COMMON_SPECS)
    if args.cmd == "run":
        specs.update(_SETTINGS_RUN_SPECS)
    elif args.cmd == "tickets" and getattr(args, "tickets_cmd", None) == "run-next":
        specs.update(_SETTINGS_TICKETS_RUN_NEXT_SPECS)
    return specs


def _settings_sections_for_args(args: argparse.Namespace) -> list[str]:
    if args.cmd == "run":
        return [_SETTINGS_SECTION_RUN_COMMON, _SETTINGS_SECTION_RUN]
    if args.cmd == "tickets" and getattr(args, "tickets_cmd", None) == "run-next":
        return [_SETTINGS_SECTION_RUN_COMMON, _SETTINGS_SECTION_TICKETS_RUN_NEXT]
    return []


def _collect_explicit_option_dests(
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> set[str]:
    option_to_dest: dict[str, str] = {}

    def _walk(current: argparse.ArgumentParser) -> None:
        for action in current._actions:
            for opt in action.option_strings:
                option_to_dest[opt] = action.dest
            if isinstance(action, argparse._SubParsersAction):
                for subparser in action.choices.values():
                    _walk(subparser)

    _walk(parser)
    explicit: set[str] = set()
    for token in argv:
        if token == "--":
            break
        if not token.startswith("-"):
            continue
        option = token.split("=", 1)[0]
        dest = option_to_dest.get(option)
        if dest:
            explicit.add(dest)
    return explicit


def _normalize_settings_for_json(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_normalize_settings_for_json(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalize_settings_for_json(item)
            for key, item in value.items()
        }
    return value


def _apply_cli_settings(
    *,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> dict[str, Any] | None:
    sections = _settings_sections_for_args(args)
    if not sections:
        return None

    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    settings_path = _resolve_settings_path(repo_root=repo_root, value=getattr(args, "settings", None))
    settings_profile = getattr(args, "settings_profile", None)

    if settings_path is None:
        if settings_profile:
            raise SystemExit(
                f"--settings-profile requires a settings file; default path not found under {repo_root}."
            )
        info = {"config_path": None, "profile": None, "applied": {}, "auto_loaded": False}
        args._settings_info = info
        return info

    if not settings_path.exists():
        raise SystemExit(f"Settings file not found: {settings_path}")

    try:
        settings_doc = _load_cli_settings_doc(settings_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    profiles_raw = settings_doc["profiles"]
    profile_name = (
        str(settings_profile).strip()
        if isinstance(settings_profile, str) and settings_profile.strip()
        else str(settings_doc.get("default_profile") or "default").strip()
    )
    if profile_name not in profiles_raw:
        raise SystemExit(
            f"Settings profile {profile_name!r} not found in {settings_path}"
        )

    profile = profiles_raw[profile_name]
    merged: dict[str, Any] = {}
    for section_name in sections:
        section = profile.get(section_name, {})
        if not isinstance(section, dict):
            raise SystemExit(
                f"Settings profile {profile_name!r} section {section_name!r} must be a mapping"
            )
        merged.update(section)

    specs = _settings_specs_for_args(args)
    unknown_keys = set(merged.keys()) - set(specs.keys())
    if unknown_keys:
        raise SystemExit(
            f"Settings profile {profile_name!r} contains unsupported keys for this command: "
            f"{sorted(unknown_keys)!r}"
        )

    explicit_dests = _collect_explicit_option_dests(parser, argv)
    applied: dict[str, Any] = {}
    for key, raw_value in merged.items():
        if key in explicit_dests:
            continue
        coerced = _coerce_settings_value(
            key=key,
            value=raw_value,
            spec=specs[key],
            settings_path=settings_path,
            repo_root=repo_root,
        )
        setattr(args, key, coerced)
        applied[key] = _normalize_settings_for_json(coerced)

    info = {
        "config_path": str(settings_path),
        "profile": profile_name,
        "applied": applied,
        "auto_loaded": getattr(args, "settings", None) is None,
    }
    args._settings_info = info
    return info




__all__ = [name for name in globals() if not name.startswith("__")]
