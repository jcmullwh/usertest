# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from runner_core import RunnerConfig
from runner_core.catalog import discover_missions, discover_personas, load_catalog_config
from runner_core.target_acquire import acquire_target

from usertest.commands.shared import (
    _coerce_string,
    _load_runner_config,
    _resolve_local_repo_root,
    _resolve_repo_root,
)


def add_lint_command(sub: argparse._SubParsersAction) -> None:
    lint_p = sub.add_parser(
        "lint",
        help="Lint missions/policies/catalog configuration (capability contract).",
    )
    lint_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    lint_p.add_argument(
        "--repo",
        help=(
            "Optional target repo input to lint catalog overrides (accepted forms: same syntax as "
            "`run --repo`). Local paths are read in-place; git URLs / pip:/pdm: "
            "inputs are acquired "
            "into a temp workspace."
        ),
    )
    lint_p.add_argument("--ref", help="Branch/tag/SHA to checkout when --repo is a git URL.")
    lint_p.add_argument(
        "--strict",
        action="store_true",
        help="Fail (non-zero exit) if any warnings are emitted.",
    )
    lint_p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Console output format.",
    )
    lint_p.add_argument(
        "--out-json",
        type=Path,
        help="Write full lint report JSON to this path (optional).",
    )
    lint_p.set_defaults(func=_cmd_lint)

def _lint__add_issue(
    issues: list[dict[str, Any]],
    *,
    severity: str,
    code: str,
    message: str,
    path: Path | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append a structured lint issue to the issue list."""
    if severity not in {"error", "warning"}:
        severity = "warning"
    issue: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if path is not None:
        issue["path"] = str(path)
    if details:
        issue["details"] = details
    issues.append(issue)


def _lint__parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """
    Parse a markdown file with leading YAML frontmatter.

    Returns (frontmatter_dict, body_md).

    Linting intentionally re-parses the source files so it can detect
    whether keys were explicitly declared vs implicitly defaulted.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"Missing YAML frontmatter in {path} (expected leading '---').")

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Invalid YAML frontmatter start in {path} (expected '---').")

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError(f"Unterminated YAML frontmatter in {path} (missing closing '---').")

    fm_text = "\n".join(lines[1:end_idx]).strip()
    body_text = "\n".join(lines[end_idx + 1 :]).strip()

    fm_raw = yaml.safe_load(fm_text) if fm_text else {}
    if fm_raw is None:
        fm_raw = {}
    if not isinstance(fm_raw, dict):
        raise ValueError(f"Expected YAML frontmatter mapping in {path}.")
    return fm_raw, body_text


_LINT_EXECUTION_HINT_RE = re.compile(
    r"\b(execute|run(?!book)|install|build|compile|test|start(?!\s+conditions)|launch|serve|cli\s+command)\b",
    re.IGNORECASE,
)
_LINT_EDIT_HINT_RE = re.compile(
    r"\b(edit|modify|patch|update|change\s+config|apply\s+change|fix\s+by\s+editing)\b",
    re.IGNORECASE,
)


def _lint__lint_policies(*, cfg: RunnerConfig, issues: list[dict[str, Any]]) -> None:
    """Validate policy configuration semantics for lint output."""
    policies = cfg.policies or {}
    if not isinstance(policies, dict):
        _lint__add_issue(
            issues,
            severity="error",
            code="policies_not_mapping",
            message="configs/policies.yaml did not parse into a 'policies' mapping.",
        )
        return

    expected = ("safe", "inspect", "write")
    for name in expected:
        if name not in policies:
            _lint__add_issue(
                issues,
                severity="warning",
                code="policy_missing",
                message=f"Policy '{name}' is missing from configs/policies.yaml.",
                details={"policy": name},
            )

    def _get_agent_section(policy_name: str, agent: str) -> dict[str, Any]:
        policy = policies.get(policy_name)
        if not isinstance(policy, dict):
            return {}
        section = policy.get(agent)
        return section if isinstance(section, dict) else {}

    def _bool_field(section: dict[str, Any], key: str, default: bool) -> bool:
        raw = section.get(key, default)
        return bool(raw) if isinstance(raw, bool) else default

    def _claude_shell(section: dict[str, Any]) -> bool:
        tools = section.get("allowed_tools")
        if not isinstance(tools, list):
            return False
        return any(isinstance(x, str) and x == "Bash" for x in tools)

    def _gemini_shell(section: dict[str, Any]) -> bool:
        tools = section.get("allowed_tools")
        if not isinstance(tools, list):
            return False
        return any(isinstance(x, str) and x == "run_shell_command" for x in tools)

    def _codex_sandbox(section: dict[str, Any]) -> str | None:
        raw = section.get("sandbox")
        return raw if isinstance(raw, str) else None

    # Enforce the core contract described in configs/policies.yaml comments:
    # safe: read-only, no shell; inspect: read-only + shell; write: edits (+ shell).
    checks = [
        ("safe", False, False),
        ("inspect", True, False),
        ("write", True, True),
    ]
    for policy_name, should_have_shell, _should_allow_edits in checks:
        for agent in ("claude", "gemini", "codex"):
            section = _get_agent_section(policy_name, agent)
            if not section:
                _lint__add_issue(
                    issues,
                    severity="warning",
                    code="policy_agent_section_missing",
                    message=f"Policy '{policy_name}' missing section for agent '{agent}'.",
                    details={"policy": policy_name, "agent": agent},
                )
                continue

            allow_edits = _bool_field(section, "allow_edits", False)

            if agent == "claude":
                has_shell = _claude_shell(section)
            elif agent == "gemini":
                has_shell = _gemini_shell(section)
            else:
                # Codex shell allowlist is not reliably inferable; only enforce edit/sandbox basics.
                has_shell = should_have_shell

            if policy_name in {"safe", "inspect"} and allow_edits:
                _lint__add_issue(
                    issues,
                    severity="error",
                    code="policy_allows_edits_in_readonly_mode",
                    message=(
                        f"Policy '{policy_name}' for agent '{agent}' has allow_edits=true, "
                        "but this policy is documented as read-only."
                    ),
                    details={"policy": policy_name, "agent": agent, "allow_edits": allow_edits},
                )

            if policy_name == "write" and not allow_edits:
                _lint__add_issue(
                    issues,
                    severity="error",
                    code="policy_write_disallows_edits",
                    message=f"Policy 'write' for agent '{agent}' has allow_edits=false.",
                    details={"policy": policy_name, "agent": agent, "allow_edits": allow_edits},
                )

            if agent in {"claude", "gemini"}:
                if should_have_shell and not has_shell:
                    _lint__add_issue(
                        issues,
                        severity="error",
                        code="policy_missing_shell_tools",
                        message=(
                            f"Policy '{policy_name}' for agent '{agent}' is expected to allow shell, "
                            "but the configured tool allowlist does not include shell."
                        ),
                        details={"policy": policy_name, "agent": agent},
                    )
                if (not should_have_shell) and has_shell:
                    _lint__add_issue(
                        issues,
                        severity="error",
                        code="policy_unexpected_shell_tools",
                        message=(
                            f"Policy '{policy_name}' for agent '{agent}' is expected to block shell, "
                            "but the configured tool allowlist enables shell."
                        ),
                        details={"policy": policy_name, "agent": agent},
                    )

            if agent == "codex":
                sandbox = _codex_sandbox(section)
                if policy_name == "write":
                    if sandbox not in {None, "workspace-write"}:
                        _lint__add_issue(
                            issues,
                            severity="warning",
                            code="codex_write_sandbox_unexpected",
                            message=(
                                "Codex policy 'write' typically uses sandbox='workspace-write'. "
                                f"Found sandbox={sandbox!r}."
                            ),
                            details={"policy": policy_name, "sandbox": sandbox},
                        )
                if policy_name in {"safe", "inspect"}:
                    if sandbox not in {None, "read-only"}:
                        _lint__add_issue(
                            issues,
                            severity="warning",
                            code="codex_readonly_sandbox_unexpected",
                            message=(
                                f"Codex policy '{policy_name}' typically uses sandbox='read-only'. "
                                f"Found sandbox={sandbox!r}."
                            ),
                            details={"policy": policy_name, "sandbox": sandbox},
                        )


def _lint__lint_catalog(
    *,
    repo_root: Path,
    target_repo_root: Path | None,
    issues: list[dict[str, Any]],
) -> None:
    """Validate catalog personas, missions, and templates for lint output."""
    catalog_config = load_catalog_config(repo_root, target_repo_root)

    try:
        personas = discover_personas(catalog_config)
        missions = discover_missions(catalog_config)
    except Exception as e:  # noqa: BLE001
        _lint__add_issue(
            issues,
            severity="error",
            code="catalog_discover_failed",
            message=str(e),
        )
        return

    # Validate defaults actually exist (load_catalog_config doesn't resolve IDs).
    if catalog_config.defaults_persona_id and catalog_config.defaults_persona_id not in personas:
        _lint__add_issue(
            issues,
            severity="error",
            code="catalog_default_persona_missing",
            message=f"defaults.persona_id={catalog_config.defaults_persona_id!r} not found in discovered personas.",
            details={"defaults.persona_id": catalog_config.defaults_persona_id},
        )
    if catalog_config.defaults_mission_id and catalog_config.defaults_mission_id not in missions:
        _lint__add_issue(
            issues,
            severity="error",
            code="catalog_default_mission_missing",
            message=f"defaults.mission_id={catalog_config.defaults_mission_id!r} not found in discovered missions.",
            details={"defaults.mission_id": catalog_config.defaults_mission_id},
        )

    # Validate prompt templates and schemas exist for every mission (prevents runtime failures).
    prompt_dir = catalog_config.prompt_templates_dir
    schema_dir = catalog_config.report_schemas_dir

    # Track explicit declaration of requirement keys per mission source.
    declared: dict[str, dict[str, Any]] = {}

    for mid, spec in missions.items():
        try:
            fm, body = _lint__parse_frontmatter(spec.source_path)
        except Exception as e:  # noqa: BLE001
            _lint__add_issue(
                issues,
                severity="error",
                code="mission_frontmatter_parse_failed",
                message=str(e),
                path=spec.source_path,
                details={"mission_id": mid},
            )
            continue

        declared[mid] = {
            "declares_requires_shell": "requires_shell" in fm,
            "declares_requires_edits": "requires_edits" in fm,
            "body": body,
            "tags": list(spec.tags),
            "extends": spec.extends,
            "source_path": spec.source_path,
        }

        # Check referenced prompt template.
        pt_rel = spec.prompt_template
        pt_path = Path(pt_rel)
        if not pt_path.is_absolute():
            pt_path = (prompt_dir / pt_path).resolve()
        if not pt_path.exists():
            _lint__add_issue(
                issues,
                severity="error",
                code="mission_prompt_template_missing",
                message=f"Mission '{mid}' references missing prompt template: {pt_path}",
                path=spec.source_path,
                details={"mission_id": mid, "prompt_template": pt_rel},
            )

        schema_rel = spec.report_schema
        schema_path = Path(schema_rel)
        if not schema_path.is_absolute():
            schema_path = (schema_dir / schema_path).resolve()
        if not schema_path.exists():
            _lint__add_issue(
                issues,
                severity="error",
                code="mission_report_schema_missing",
                message=f"Mission '{mid}' references missing report schema: {schema_path}",
                path=spec.source_path,
                details={"mission_id": mid, "report_schema": schema_rel},
            )

    # Now that we've parsed all missions, enforce explicit requirement declaration
    # somewhere in the extends chain (prevents silent default=false that breaks preflight validation).
    for mid, spec in missions.items():
        meta = declared.get(mid)
        if meta is None:
            continue

        def _chain_declares(flag_key: str, *, start_mid: str = mid) -> bool:
            cur: str | None = start_mid
            seen: set[str] = set()
            while cur and cur not in seen:
                seen.add(cur)
                m = declared.get(cur)
                if m and bool(m.get(flag_key)):
                    return True
                cur = missions[cur].extends
            return False

        has_shell_decl = _chain_declares("declares_requires_shell")
        has_edits_decl = _chain_declares("declares_requires_edits")

        # Escalate to error when the mission text strongly implies execution/editing.
        body = str(meta.get("body") or "")
        tags = set(str(t) for t in (meta.get("tags") or []))

        implies_shell = (
            bool(_LINT_EXECUTION_HINT_RE.search(body)) or ("p0" in tags) or ("onboarding" in tags)
        )
        implies_edits = bool(_LINT_EDIT_HINT_RE.search(body))

        if not has_shell_decl:
            _lint__add_issue(
                issues,
                severity=("error" if implies_shell else "warning"),
                code="mission_requires_shell_undeclared",
                message=(
                    f"Mission '{mid}' does not explicitly declare requires_shell (defaults to false). "
                    "Add `requires_shell: true|false` to mission YAML frontmatter so preflight/matrix validation is reliable."
                ),
                path=spec.source_path,
                details={
                    "mission_id": mid,
                    "extends": spec.extends,
                    "implies_shell": implies_shell,
                },
            )

        if not has_edits_decl:
            _lint__add_issue(
                issues,
                severity=("error" if implies_edits else "warning"),
                code="mission_requires_edits_undeclared",
                message=(
                    f"Mission '{mid}' does not explicitly declare requires_edits (defaults to false). "
                    "Add `requires_edits: true|false` to mission YAML frontmatter so preflight/matrix validation is reliable."
                ),
                path=spec.source_path,
                details={
                    "mission_id": mid,
                    "extends": spec.extends,
                    "implies_edits": implies_edits,
                },
            )

        # Heuristic sanity-check: explicit false + strong implication => warn.
        if has_shell_decl and not bool(getattr(spec, "requires_shell", False)) and implies_shell:
            _lint__add_issue(
                issues,
                severity="warning",
                code="mission_requires_shell_maybe_wrong",
                message=(
                    f"Mission '{mid}' has requires_shell=false, but the mission text/tags suggests execution. "
                    "Confirm that the mission is intended to work in --policy safe (no shell)."
                ),
                path=spec.source_path,
                details={"mission_id": mid},
            )
        if has_edits_decl and not bool(getattr(spec, "requires_edits", False)) and implies_edits:
            _lint__add_issue(
                issues,
                severity="warning",
                code="mission_requires_edits_maybe_wrong",
                message=(
                    f"Mission '{mid}' has requires_edits=false, but the mission text suggests editing. "
                    "Confirm whether it should require --policy write."
                ),
                path=spec.source_path,
                details={"mission_id": mid},
            )


def _cmd_lint(args: argparse.Namespace) -> int:
    """Execute the lint subcommand."""
    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    cfg = _load_runner_config(repo_root)

    target_repo_root: Path | None = None
    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None

    repo_input = _coerce_string(getattr(args, "repo", None))
    if repo_input is not None:
        # Prefer linting the real local repo if it exists (no cloning/copying needed).
        local = _resolve_local_repo_root(repo_root, repo_input)
        if local is not None:
            target_repo_root = local
        else:
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="usertest_lint_")
            dest = Path(temp_dir_obj.name) / "target"
            acquired = acquire_target(
                repo=repo_input, dest_dir=dest, ref=_coerce_string(getattr(args, "ref", None))
            )
            target_repo_root = acquired.workspace_dir

    issues: list[dict[str, Any]] = []
    _lint__lint_policies(cfg=cfg, issues=issues)
    _lint__lint_catalog(repo_root=repo_root, target_repo_root=target_repo_root, issues=issues)

    # Sort issues for stable output.
    severity_rank = {"error": 0, "warning": 1}
    issues.sort(
        key=lambda x: (
            severity_rank.get(str(x.get("severity")), 9),
            str(x.get("code")),
            str(x.get("path") or ""),
        )
    )

    totals = {
        "errors": sum(1 for i in issues if i.get("severity") == "error"),
        "warnings": sum(1 for i in issues if i.get("severity") == "warning"),
        "issues": len(issues),
    }

    report = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repo_root": str(repo_root),
            "target_repo_root": str(target_repo_root) if target_repo_root is not None else None,
            "repo": repo_input,
            "ref": _coerce_string(getattr(args, "ref", None)),
        },
        "totals": totals,
        "issues": issues,
    }

    out_json = getattr(args, "out_json", None)
    if out_json is not None:
        out_path = out_json
        if not out_path.is_absolute():
            out_path = (repo_root / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(str(out_path))

    fmt = str(getattr(args, "format", "text"))
    if fmt == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"lint errors: {totals['errors']}")
        print(f"lint warnings: {totals['warnings']}")
        if issues:
            for issue in issues:
                sev = issue.get("severity")
                code = issue.get("code")
                path = issue.get("path")
                msg = issue.get("message")
                loc = f" ({path})" if path else ""
                print(f"- [{sev}] {code}{loc}: {msg}")

    if temp_dir_obj is not None:
        temp_dir_obj.cleanup()

    fail_on_warn = bool(getattr(args, "strict", False))
    if totals["errors"] > 0:
        return 2
    if fail_on_warn and totals["warnings"] > 0:
        return 2
    return 0

__all__ = ['add_lint_command', '_cmd_lint']
