"""Content-addressed stage-6 target intent and lightweight implementation-scope checks."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

_PLAN_TARGET_CONTRACT_RE = re.compile(
    r"<!-- backlog-plan-target-contract:start -->\s*"
    r"```json\s*(?P<payload>\{.*?\})\s*```\s*"
    r"<!-- backlog-plan-target-contract:end -->",
    flags=re.DOTALL,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"plan_target_contract_{label}_invalid")
    return value.strip()


def _safe_relative_path(value: Any) -> str:
    raw = _required_text(value, label="path").replace("\\", "/").removeprefix("./")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or ".." in posix.parts
        or any(ord(character) < 32 for character in raw)
    ):
        raise ValueError(f"plan_target_contract_path_unsafe:{raw}")
    return posix.as_posix()


def _git_head(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    value = proc.stdout.strip()
    if proc.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise ValueError("plan_target_contract_repository_head_unavailable")
    return value


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    parts = PurePosixPath(normalized).parts
    filename = parts[-1] if parts else normalized
    return bool(
        "tests" in parts
        or "test" in parts[:-1]
        or filename.startswith("test_")
        or filename.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
    )


def build_plan_target_contract(
    plan: Mapping[str, Any],
    *,
    repo_root: Path,
) -> dict[str, Any]:
    """Bind model-planned intervention intent to the exact researched revision."""

    root = repo_root.expanduser().resolve()
    repo_revision = _required_text(plan.get("repo_revision"), label="repo_revision")
    head_revision = _git_head(root)
    if repo_revision.casefold() != head_revision.casefold():
        raise ValueError(
            "plan_target_contract_repository_revision_mismatch:"
            f"expected={repo_revision}:head={head_revision}"
        )
    targets_raw = plan.get("change_targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ValueError("plan_target_contract_targets_missing")
    targets: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw in enumerate(targets_raw):
        if not isinstance(raw, Mapping):
            raise ValueError(f"plan_target_contract_target_invalid:{index}")
        action = _required_text(raw.get("action"), label="action")
        if action not in {"modify", "create"}:
            raise ValueError(f"plan_target_contract_action_invalid:{index}")
        relative_path = _safe_relative_path(raw.get("path"))
        if relative_path in seen_paths:
            raise ValueError(f"plan_target_contract_path_duplicate:{relative_path}")
        seen_paths.add(relative_path)
        symbols_raw = raw.get("symbols")
        if not isinstance(symbols_raw, list) or not symbols_raw:
            raise ValueError(f"plan_target_contract_symbols_invalid:{relative_path}")
        symbols = [_required_text(symbol, label="symbol") for symbol in symbols_raw]
        if len(symbols) != len(set(symbols)):
            raise ValueError(f"plan_target_contract_symbol_duplicate:{relative_path}")
        intervention = _required_text(raw.get("change"), label="change")
        targets.append(
            {
                "action": action,
                "path": relative_path,
                "symbols": symbols,
                "change": intervention,
                "change_sha256": _sha256_text(intervention),
                "target_role": "test" if _is_test_path(relative_path) else "production",
            }
        )
    payload = {
        "schema_version": 2,
        "contract_source": "runner_stage6_target_intent_v2",
        "case_id": _required_text(plan.get("case_id"), label="case_id"),
        "problem_id": _required_text(plan.get("problem_id"), label="problem_id"),
        "selected_option_id": _required_text(
            plan.get("selected_option_id"), label="selected_option_id"
        ),
        "repo_revision": repo_revision,
        "targets": targets,
    }
    return {**payload, "contract_sha256": _sha256_text(_canonical_json(payload))}


def validate_plan_target_contract(contract: Any) -> dict[str, Any]:
    """Validate a persisted target-intent contract and return a normalized copy."""

    if not isinstance(contract, Mapping) or contract.get("schema_version") != 2:
        raise ValueError("plan_target_contract_schema_invalid")
    allowed = {
        "schema_version",
        "contract_source",
        "case_id",
        "problem_id",
        "selected_option_id",
        "repo_revision",
        "targets",
        "contract_sha256",
    }
    if set(contract) != allowed:
        raise ValueError("plan_target_contract_fields_invalid")
    if contract.get("contract_source") != "runner_stage6_target_intent_v2":
        raise ValueError("plan_target_contract_source_invalid")
    payload = {key: contract[key] for key in contract if key != "contract_sha256"}
    if contract.get("contract_sha256") != _sha256_text(_canonical_json(payload)):
        raise ValueError("plan_target_contract_hash_mismatch")
    for field in ("case_id", "problem_id", "selected_option_id", "repo_revision"):
        _required_text(contract.get(field), label=field)
    targets = contract.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("plan_target_contract_targets_invalid")
    paths: set[str] = set()
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            raise ValueError(f"plan_target_contract_target_invalid:{index}")
        if set(target) != {
            "action",
            "path",
            "symbols",
            "change",
            "change_sha256",
            "target_role",
        }:
            raise ValueError(f"plan_target_contract_target_fields_invalid:{index}")
        path = _safe_relative_path(target.get("path"))
        if path in paths:
            raise ValueError(f"plan_target_contract_path_duplicate:{path}")
        paths.add(path)
        if target.get("action") not in {"modify", "create"}:
            raise ValueError(f"plan_target_contract_action_invalid:{path}")
        symbols = target.get("symbols")
        if (
            not isinstance(symbols, list)
            or not symbols
            or len(symbols) != len(set(symbols))
            or not all(isinstance(symbol, str) and symbol.strip() for symbol in symbols)
        ):
            raise ValueError(f"plan_target_contract_symbols_invalid:{path}")
        intervention = _required_text(target.get("change"), label="change")
        if target.get("change_sha256") != _sha256_text(intervention):
            raise ValueError(f"plan_target_contract_change_hash_mismatch:{path}")
        expected_role = "test" if _is_test_path(path) else "production"
        if target.get("target_role") != expected_role:
            raise ValueError(f"plan_target_contract_target_role_invalid:{path}")
    return json.loads(_canonical_json(contract))


def render_plan_target_contract_markdown(contract: Any) -> str:
    normalized = validate_plan_target_contract(contract)
    return (
        "<!-- backlog-plan-target-contract:start -->\n"
        "```json\n"
        + json.dumps(normalized, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n```\n"
        "<!-- backlog-plan-target-contract:end -->"
    )


def parse_plan_target_contract_markdown(markdown: str) -> dict[str, Any] | None:
    matches = list(_PLAN_TARGET_CONTRACT_RE.finditer(markdown))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("plan_target_contract_block_ambiguous")
    try:
        payload = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        raise ValueError("plan_target_contract_json_invalid") from exc
    return validate_plan_target_contract(payload)


def assess_pr_plan_scope(
    *,
    contract: Any,
    changed_files: list[str],
    diff_text: str,
    reviewed_head_oid: str,
    verified_implementation_head: str,
) -> dict[str, Any]:
    """Block only missing production targets or an unverified reviewed head.

    Extra paths, untouched test targets, and symbol/hunk breadth remain visible as
    advisories for semantic model review; they are intentionally not machine vetoes.
    """

    normalized = validate_plan_target_contract(contract)
    expected = {
        str(target["path"]): target
        for target in normalized["targets"]
        if isinstance(target, dict)
    }
    observed_paths = [path.replace("\\", "/").removeprefix("./") for path in changed_files]
    observed_set = set(observed_paths)
    production_paths = {
        path for path, target in expected.items() if target.get("target_role") == "production"
    }
    missing_production = sorted(production_paths - observed_set)
    missing_test_or_support = sorted((set(expected) - production_paths) - observed_set)
    unplanned_paths = sorted(observed_set - set(expected))
    errors = [
        f"implementation_scope_required_production_path_untouched:{path}"
        for path in missing_production
    ]
    if reviewed_head_oid.casefold() != verified_implementation_head.casefold():
        errors.append(
            "implementation_scope_reviewed_head_not_verified:"
            f"reviewed={reviewed_head_oid}:verified={verified_implementation_head}"
        )
    advisories = [
        *[f"implementation_scope_unplanned_path:{path}" for path in unplanned_paths],
        *[
            f"implementation_scope_planned_nonproduction_path_untouched:{path}"
            for path in missing_test_or_support
        ],
    ]
    target_receipts = [
        {
            "path": path,
            "target_role": target["target_role"],
            "path_touched": path in observed_set,
            "planned_symbols": list(target["symbols"]),
            "planned_intervention_sha256": target["change_sha256"],
        }
        for path, target in sorted(expected.items())
    ]
    payload = {
        "schema_version": 2,
        "target_contract_sha256": normalized["contract_sha256"],
        "repo_revision": normalized["repo_revision"],
        "reviewed_head_oid": reviewed_head_oid,
        "verified_implementation_head": verified_implementation_head,
        "changed_files": observed_paths,
        "changed_files_sha256": _sha256_text(_canonical_json(observed_paths)),
        "diff_sha256": _sha256_text(diff_text),
        "target_receipts": target_receipts,
        "errors": errors,
        "advisories": advisories,
    }
    return {
        **payload,
        "status": "verified" if not errors else "failed",
        "receipt_sha256": _sha256_text(_canonical_json(payload)),
    }


__all__ = [
    "assess_pr_plan_scope",
    "build_plan_target_contract",
    "parse_plan_target_contract_markdown",
    "render_plan_target_contract_markdown",
    "validate_plan_target_contract",
]
