"""Depth-oriented contracts shared by solution optioning and planning stages.

The generic stage parsers intentionally preserve malformed model output for audit.  The
helpers in this module add the stricter progression rules needed by stages 4-6: only
evidence-backed, distinct options and decision-complete implementation plans are allowed
to advance.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
import tomllib
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml
from backlog_core import (
    assess_change_plan_readiness,
    assess_selection_readiness,
    assess_solution_option_readiness,
    falsification_review_receipt_errors,
    plan_revision_id_for,
    research_limitation_references,
    verified_mechanism_evidence,
)
from backlog_core.stage_contracts import parse_solution_option_sets
from backlog_core.ticket_readiness import falsification_acceptance_has_adversarial_basis
from runner_core import verification_command_safety_errors

_OPTIONING_STATUSES = frozenset({"options_produced", "insufficient_evidence", "no_safe_option"})
_SCOPE_LEVELS = frozenset({"single_path", "multiple_independent_paths", "shared_abstraction"})
_MECHANISM_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CLASS_SCOPE_RE = re.compile(
    r"\b(?:canonical|central(?:ize|ized|ization)|class[- ]level|shared(?:\s+internal)?"
    r"\s+(?:abstraction|contract|mechanism|source)|system[- ]wide|all\s+(?:callers|consumers))\b",
    re.IGNORECASE,
)
_DISCOVERY_FIRST_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?"
    r"(?:locate|identify|determine|inspect|audit|review|investigate|find|explore|"
    r"discover|decide|choose|assess)\b",
    re.IGNORECASE,
)


def _mechanisms_too_similar(left: str, right: str) -> bool:
    """Reject reordered or lightly rephrased copies of one mechanism."""
    left_tokens = _MECHANISM_TOKEN_RE.findall(left.casefold())
    right_tokens = _MECHANISM_TOKEN_RE.findall(right.casefold())
    if not left_tokens or not right_tokens:
        return False
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    jaccard = len(left_set & right_set) / len(left_set | right_set)
    sequence = SequenceMatcher(None, " ".join(left_tokens), " ".join(right_tokens)).ratio()
    return jaccard >= 0.8 or sequence >= 0.9


_VAGUE_COMMAND_RE = re.compile(
    r"\b(?:relevant|appropriate|as needed|tbd|todo|<[^>]+>)\b", re.IGNORECASE
)
_VERIFICATION_TOOL_RE = re.compile(
    r"^\s*(?:pdm|uv|pytest|python\s+-m\s+pytest|python|npm|pnpm|yarn|cargo|"
    r"go\s+test|dotnet\s+test|mvn|gradle|make|cmake|bash|powershell)\b",
    re.IGNORECASE,
)
_COMMAND_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.{}-]+(?:\.[A-Za-z0-9]+)?)"
)


def read_repo_revision(repo_root: Path) -> str:
    """Return the exact checked-out Git revision used for repository inspection.

    A missing revision is a hard error: an option or plan that cannot name the source
    revision it inspected is not reproducible enough to advance.
    """

    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or not revision:
        detail = result.stderr.strip() or "git rev-parse returned no revision"
        raise RuntimeError(f"repository_revision_unavailable: {repo_root}: {detail}")
    return revision


def repo_contains_revision(repo_root: Path, revision: str) -> bool:
    """Return whether the read-only workspace can inspect an exact Git revision."""

    if not revision.strip() or revision.casefold().startswith("unavailable"):
        return False
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def assess_repo_grounding(repo_root: Path, revision: str) -> tuple[bool, list[str], dict[str, Any]]:
    """Require a clean checkout whose HEAD is exactly the research revision."""

    reasons: list[str] = []
    try:
        head_revision = read_repo_revision(repo_root)
    except RuntimeError as exc:
        return (
            False,
            [str(exc)],
            {
                "workspace": str(repo_root.resolve()),
                "requested_revision": revision,
                "access": "read_only",
                "clean": False,
            },
        )
    if head_revision.casefold() != revision.strip().casefold():
        reasons.append(f"workspace_head_mismatch: expected={revision!r} actual={head_revision!r}")
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if status.returncode != 0:
        reasons.append("workspace_cleanliness_unavailable")
    elif status.stdout.strip():
        reasons.append("workspace_has_uncommitted_changes")
    context: dict[str, Any] = {
        "workspace": str(repo_root.resolve()),
        "requested_revision": revision,
        "head_revision": head_revision,
        "access": "read_only",
        "clean": not bool(status.stdout.strip()) and status.returncode == 0,
    }
    return not reasons, reasons, context


def read_only_stage_tools(agent: str) -> list[str]:
    """Return a backend-specific allowlist containing only repository read tools."""

    if agent == "claude":
        return ["Read", "Grep", "Glob"]
    if agent == "gemini":
        return ["read_file", "search_file_content"]
    return []


def stage_include_directories(agent: str, repo_root: Path) -> list[str]:
    """Return the repository include directory required by the Gemini backend."""

    return [str(repo_root)] if agent == "gemini" else []


def repo_context_payload(repo_root: Path, revision: str) -> dict[str, str]:
    """Build the immutable repository context injected into stage prompts."""

    return {
        "workspace": str(repo_root.resolve()),
        "requested_revision": revision,
        "head_revision": read_repo_revision(repo_root),
        "access": "read_only",
    }


def _json_value(raw_text: str) -> Any:
    """Extract a JSON value from strict JSON or a single fenced response."""

    text = raw_text.strip()
    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 : -3].strip()
    return json.loads(text)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: Any, *, allow_empty: bool = True) -> bool:
    if not isinstance(value, list):
        return False
    if not allow_empty and not value:
        return False
    return all(_nonempty_string(item) for item in value)


def _option_quality_errors(
    option: dict[str, Any], *, research_dossier: dict[str, Any] | None = None
) -> list[str]:
    """Return progression-blocking quality errors for one solution option."""

    option_id = option.get("option_id") or "(no option_id)"
    errors: list[str] = []
    if _nonempty_string(option.get("_parse_warning")):
        errors.append(f"option_stage_contract_invalid: {option_id}")

    coverage = option.get("causal_coverage")
    if not isinstance(coverage, dict):
        return [f"option_missing_causal_coverage: {option_id}"]

    if not _nonempty_string(coverage.get("mechanism_addressed")):
        errors.append(f"option_missing_mechanism_addressed: {option_id}")
    for field, allow_empty in (
        ("symptoms_covered", False),
        ("unsupported_assumptions", True),
        ("residual_recurrence_paths", True),
        ("compatibility_risks", True),
    ):
        if not _string_list(coverage.get(field), allow_empty=allow_empty):
            errors.append(f"option_invalid_causal_coverage_{field}: {option_id}")
    testability = coverage.get("testability")
    if not isinstance(testability, dict) or not _nonempty_string(testability.get("before")):
        errors.append(f"option_missing_testability_before: {option_id}")
    if not isinstance(testability, dict) or not _nonempty_string(testability.get("after")):
        errors.append(f"option_missing_testability_after: {option_id}")

    scope = option.get("scope_evidence")
    if not isinstance(scope, dict):
        return [*errors, f"option_missing_scope_evidence: {option_id}"]
    scope_level = scope.get("scope_level")
    if scope_level not in _SCOPE_LEVELS:
        errors.append(f"option_invalid_scope_level: {option_id}: {scope_level!r}")

    paths = scope.get("independent_consumers_or_failure_paths")
    if not isinstance(paths, list) or not paths:
        errors.append(f"option_missing_scope_paths: {option_id}")
        paths = []

    names: set[str] = set()
    for index, path in enumerate(paths):
        if not isinstance(path, dict):
            errors.append(f"option_invalid_scope_path: {option_id}: index={index}")
            continue
        name = path.get("name")
        evidence_refs = path.get("evidence_refs")
        if not _nonempty_string(name):
            errors.append(f"option_missing_scope_path_name: {option_id}: index={index}")
        else:
            names.add(str(name).strip().casefold())
        if not _string_list(evidence_refs, allow_empty=False):
            errors.append(f"option_missing_scope_evidence_refs: {option_id}: index={index}")

    class_claim_text = " ".join(
        str(option.get(field) or "") for field in ("summary", "rationale", "recurrence_prevention")
    )
    claims_class_scope = bool(_CLASS_SCOPE_RE.search(class_claim_text))
    broad_scope = scope_level in {"multiple_independent_paths", "shared_abstraction"}
    if claims_class_scope and not broad_scope:
        errors.append(f"option_class_claim_without_broad_scope_evidence: {option_id}")
    if broad_scope and len(names) < 2:
        errors.append(f"option_class_scope_requires_two_independent_paths: {option_id}")

    authoritative_ready, authoritative_reasons = assess_solution_option_readiness(
        option,
        research=research_dossier,
    )
    if not authoritative_ready:
        errors.extend(authoritative_reasons)
    return list(dict.fromkeys(errors))


def parse_optioning_response(
    raw_text: str,
    *,
    expected_problem_id: str,
    known_family_ids: set[str],
    research_dossier: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Parse a stage-4 envelope and reject shallow or duplicate mechanism options.

    The envelope explicitly represents a zero-option outcome. Live stage output must
    use that envelope; legacy bare arrays are rejected rather than silently promoted.
    """

    raw = _json_value(raw_text)
    if isinstance(raw, list):
        raise ValueError("solution_optioner_legacy_bare_array_forbidden")
    if isinstance(raw, dict):
        status = str(raw.get("optioning_status") or "").strip()
        rationale = str(raw.get("decision_rationale") or "").strip()
        raw_options = raw.get("options")
    else:
        raise ValueError(f"solution_optioner_response_invalid_type: {type(raw).__name__}")

    if status not in _OPTIONING_STATUSES:
        raise ValueError(f"solution_optioner_invalid_status: {status!r}")
    if not rationale:
        raise ValueError("solution_optioner_missing_decision_rationale")
    if not isinstance(raw_options, list):
        raise ValueError("solution_optioner_options_not_a_list")
    if len(raw_options) > 3:
        raise ValueError(f"solution_optioner_too_many_options: expected<=3 got={len(raw_options)}")
    if status in {"insufficient_evidence", "no_safe_option"} and raw_options:
        raise ValueError(f"solution_optioner_{status}_must_have_zero_options")
    if status == "options_produced" and not raw_options:
        raise ValueError("solution_optioner_options_produced_requires_option")

    parsed, warnings = parse_solution_option_sets(
        json.dumps(raw_options, ensure_ascii=False), known_family_ids=known_family_ids
    )
    valid: list[dict[str, Any]] = []
    seen_option_ids: set[str] = set()
    seen_mechanisms: list[str] = []
    rejected = 0

    for option in parsed:
        option_errors = list(_option_quality_errors(option, research_dossier=research_dossier))
        problem_id = option.get("problem_id")
        if problem_id != expected_problem_id:
            option_errors.append(
                "solution_option_problem_id_mismatch: "
                f"expected={expected_problem_id} got={problem_id!r}"
            )
        option_id = str(option.get("option_id") or "").strip()
        if option_id in seen_option_ids:
            option_errors.append(
                f"solution_option_duplicate_id: {expected_problem_id}: {option_id}"
            )
        mechanism = ""
        coverage = option.get("causal_coverage")
        if isinstance(coverage, dict):
            mechanism = re.sub(
                r"[^a-z0-9]+",
                " ",
                str(coverage.get("mechanism_addressed") or "").casefold(),
            ).strip()
        if mechanism and any(
            _mechanisms_too_similar(mechanism, previous) for previous in seen_mechanisms
        ):
            option_errors.append(
                f"solution_option_duplicate_mechanism: {expected_problem_id}: {mechanism}"
            )

        if option_errors:
            warnings.extend(option_errors)
            rejected += 1
            continue
        seen_option_ids.add(option_id)
        seen_mechanisms.append(mechanism)
        valid.append(option)

    effective_status = status
    if status == "options_produced" and not valid:
        effective_status = "invalid_output"
    outcome = {
        "problem_id": expected_problem_id,
        "optioning_status": effective_status,
        "decision_rationale": rationale,
        "option_count": len(valid),
        "rejected_option_count": rejected,
    }
    return outcome, valid, warnings


def selection_quality_errors(
    decision: dict[str, Any],
    *,
    expected_problem_id: str,
    options_by_id: dict[str, dict[str, Any]],
    research_dossier: dict[str, Any] | None = None,
    require_complete: bool = False,
) -> list[str]:
    """Return errors that make a stage-5 selection unsafe to advance."""

    errors: list[str] = []
    if _nonempty_string(decision.get("_parse_warning")):
        errors.append(f"selection_stage_contract_invalid: {expected_problem_id}")
    if decision.get("problem_id") != expected_problem_id:
        errors.append(
            "selection_problem_id_mismatch: "
            f"expected={expected_problem_id} got={decision.get('problem_id')!r}"
        )
    option_id = str(decision.get("selected_option_id") or "").strip()
    selected = options_by_id.get(option_id)
    if selected is None:
        errors.append(f"selection_unknown_option: {expected_problem_id}: {option_id!r}")
        return errors
    if decision.get("selected_family_id") != selected.get("family_id"):
        errors.append(f"selection_family_mismatch: {expected_problem_id}: {option_id}")

    evaluation = decision.get("causal_coverage_evaluation")
    if not isinstance(evaluation, dict):
        errors.append(f"selection_missing_causal_coverage_evaluation: {expected_problem_id}")
        return errors
    if not _nonempty_string(evaluation.get("mechanism_fit")):
        errors.append(f"selection_missing_mechanism_fit: {expected_problem_id}")
    for field in ("accepted_unsupported_assumptions", "accepted_residual_risks"):
        if not _string_list(evaluation.get(field), allow_empty=True):
            errors.append(f"selection_invalid_{field}: {expected_problem_id}")
    if not isinstance(evaluation.get("class_level_evidence_sufficient"), bool):
        errors.append(f"selection_missing_class_level_evidence_decision: {expected_problem_id}")
    if require_complete:
        authoritative_ready, authoritative_reasons = assess_selection_readiness(
            decision,
            options=list(options_by_id.values()),
            research=research_dossier,
        )
        if not authoritative_ready:
            errors.extend(authoritative_reasons)
    return list(dict.fromkeys(errors))


def falsification_review_errors(
    review: dict[str, Any],
    *,
    expected_problem_id: str,
    expected_option_id: str,
    research_dossier: dict[str, Any] | None = None,
    selected_option: dict[str, Any] | None = None,
) -> list[str]:
    """Validate the independent stage-5 falsification review."""

    errors: list[str] = []
    if review.get("problem_id") != expected_problem_id:
        errors.append(f"falsification_problem_id_mismatch: {expected_problem_id}")
    if review.get("selected_option_id") != expected_option_id:
        errors.append(f"falsification_option_id_mismatch: {expected_option_id}")
    if review.get("verdict") not in {"accept", "reject", "insufficient_evidence"}:
        errors.append(f"falsification_invalid_verdict: {review.get('verdict')!r}")
    for field in (
        "strongest_counterargument",
        "evidence_that_would_change_verdict",
    ):
        if not _nonempty_string(review.get(field)):
            errors.append(f"falsification_missing_{field}: {expected_problem_id}")
    for field in ("unsupported_assumptions", "residual_risks"):
        if not _string_list(review.get(field), allow_empty=True):
            errors.append(f"falsification_invalid_{field}: {expected_problem_id}")
    if isinstance(selected_option, dict):
        errors.extend(
            falsification_review_receipt_errors(
                review,
                problem_id=expected_problem_id,
                selected_option=selected_option,
                research=research_dossier,
            )
        )
    allowed_refs = set(verified_mechanism_evidence(research_dossier))
    evidence_refs = review.get("evidence_refs")
    adversarial_refs: set[str] = set()
    if not isinstance(evidence_refs, list) or not evidence_refs:
        errors.append(f"falsification_missing_evidence_refs: {expected_problem_id}")
    else:
        for index, evidence_ref in enumerate(evidence_refs):
            if not isinstance(evidence_ref, dict):
                errors.append(f"falsification_invalid_evidence_ref: index={index}")
                continue
            ref = str(evidence_ref.get("ref") or "").strip()
            if not ref or ref not in allowed_refs:
                errors.append(f"falsification_unbound_evidence_ref: index={index}: {ref!r}")
            if not _nonempty_string(evidence_ref.get("finding")):
                errors.append(f"falsification_missing_evidence_finding: index={index}")
            if evidence_ref.get("effect") not in {
                "supports_selection",
                "challenges_selection",
                "limits_scope",
            }:
                errors.append(f"falsification_invalid_evidence_effect: index={index}")
            elif evidence_ref.get("effect") in {"challenges_selection", "limits_scope"}:
                adversarial_refs.add(ref)
    has_adversarial_basis = falsification_acceptance_has_adversarial_basis(review)
    if review.get("verdict") == "accept" and not has_adversarial_basis:
        errors.append("falsification_accept_without_adversarial_evidence")
    critical_findings = review.get("critical_findings")
    if not isinstance(critical_findings, list):
        errors.append(f"falsification_invalid_critical_findings: {expected_problem_id}")
    else:
        for index, finding in enumerate(critical_findings):
            refs_raw = finding.get("evidence_refs") if isinstance(finding, dict) else None
            refs = (
                [ref.strip() for ref in refs_raw if isinstance(ref, str) and ref.strip()]
                if isinstance(refs_raw, list)
                else []
            )
            if (
                not isinstance(finding, dict)
                or not _nonempty_string(finding.get("finding"))
                or finding.get("affects")
                not in {"root_cause", "interface", "change_surface"}
                or not refs
                or any(ref not in allowed_refs for ref in refs)
            ):
                errors.append(f"falsification_invalid_critical_finding: index={index}")
        if review.get("verdict") == "accept" and critical_findings:
            errors.append("falsification_accepts_critical_finding")

    material_risks: set[str] = set()
    coverage = selected_option.get("causal_coverage") if isinstance(selected_option, dict) else None
    if isinstance(coverage, dict):
        for field in (
            "unsupported_assumptions",
            "residual_recurrence_paths",
            "compatibility_risks",
        ):
            values = coverage.get(field)
            if isinstance(values, list):
                material_risks.update(
                    value.strip() for value in values if isinstance(value, str) and value.strip()
                )
    for field in ("unsupported_assumptions", "residual_risks"):
        values = review.get(field)
        if isinstance(values, list):
            material_risks.update(
                value.strip() for value in values if isinstance(value, str) and value.strip()
            )
    dispositions = review.get("material_risk_dispositions")
    if not isinstance(dispositions, list):
        errors.append(f"falsification_invalid_material_risk_dispositions: {expected_problem_id}")
    else:
        disposed: set[str] = set()
        for index, disposition in enumerate(dispositions):
            if not isinstance(disposition, dict):
                errors.append(f"falsification_invalid_risk_disposition: index={index}")
                continue
            risk = str(disposition.get("risk") or "").strip()
            decision = disposition.get("disposition")
            if not risk or risk not in material_risks:
                errors.append(f"falsification_unbound_material_risk: index={index}: {risk!r}")
            else:
                if risk in disposed:
                    errors.append(f"falsification_duplicate_risk_disposition: index={index}")
                disposed.add(risk)
            if decision not in {"accepted", "mitigated", "blocks_selection"}:
                errors.append(f"falsification_invalid_risk_disposition_value: index={index}")
            if decision == "blocks_selection" and review.get("verdict") == "accept":
                errors.append("falsification_accepts_blocking_material_risk")
            refs_raw = disposition.get("evidence_refs")
            refs = (
                [ref.strip() for ref in refs_raw if isinstance(ref, str) and ref.strip()]
                if isinstance(refs_raw, list)
                else []
            )
            if (
                not refs
                or len(refs) != len(refs_raw)
                or any(ref not in allowed_refs for ref in refs)
            ):
                errors.append(f"falsification_unbound_risk_evidence: index={index}")
            elif decision == "mitigated" and not (
                adversarial_refs.intersection(refs) or has_adversarial_basis
            ):
                errors.append(
                    f"falsification_mitigation_lacks_adversarial_evidence: index={index}"
                )
            if not _nonempty_string(disposition.get("rationale")):
                errors.append(f"falsification_missing_risk_rationale: index={index}")
        missing = sorted(material_risks - disposed)
        if missing:
            errors.append("falsification_undisposed_material_risks: " + ", ".join(missing))
    return errors


def _path_within_repo(repo_root: Path, raw_path: str) -> Path | None:
    path = Path(raw_path)
    if path.is_absolute():
        return None
    try:
        resolved = (repo_root / path).resolve()
        resolved.relative_to(repo_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _python_target_bindings(content: str) -> set[str]:
    """Return concrete Python definitions, assignments, and import bindings."""

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    bindings: set[str] = set()

    def add(name: str, prefix: tuple[str, ...]) -> None:
        bindings.add(".".join((*prefix, name)))

    def visit(nodes: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = (*prefix, node.name)
                bindings.add(".".join(qualified))
                visit(node.body, qualified)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        add(target.id, prefix)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                add(node.target.id, prefix)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.asname or alias.name.split(".", 1)[0], prefix)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        add(alias.asname or alias.name, prefix)

    visit(tree.body)
    return bindings


def _config_pointer_segments(symbol: str) -> list[str] | None:
    if not symbol.startswith("config:/"):
        return None
    raw_segments = symbol.removeprefix("config:/").split("/")
    if not raw_segments or any(not segment for segment in raw_segments):
        return None
    decoded_segments: list[str] = []
    for raw in raw_segments:
        decoded: list[str] = []
        index = 0
        while index < len(raw):
            if raw[index] != "~":
                decoded.append(raw[index])
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                return None
            decoded.append("~" if raw[index + 1] == "0" else "/")
            index += 2
        decoded_segments.append("".join(decoded))
    return decoded_segments


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate config key: {key}")
        value[key] = item
    return value


def _config_value_at_pointer(value: Any, segments: list[str]) -> bool:
    current = value
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                return False
            current = current[segment]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", segment):
                return False
            index = int(segment)
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _yaml_pointer_exists(content: str, segments: list[str]) -> bool:
    try:
        current: Any = yaml.compose(content, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return False
    if current is None:
        return False
    for segment in segments:
        if isinstance(current, yaml.MappingNode):
            matches = [
                value_node
                for key_node, value_node in current.value
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == segment
            ]
            if len(matches) != 1:
                return False
            current = matches[0]
        elif isinstance(current, yaml.SequenceNode):
            if not re.fullmatch(r"0|[1-9][0-9]*", segment):
                return False
            index = int(segment)
            if index >= len(current.value):
                return False
            current = current.value[index]
        else:
            return False
    return True


def _config_pointer_exists(*, path: Path, content: str, symbol: str) -> bool:
    segments = _config_pointer_segments(symbol)
    if segments is None:
        return False
    try:
        if path.suffix.casefold() == ".json":
            value = json.loads(content, object_pairs_hook=_unique_json_object)
        elif path.suffix.casefold() == ".toml":
            value = tomllib.loads(content)
        elif path.suffix.casefold() in {".yaml", ".yml"}:
            return _yaml_pointer_exists(content, segments)
        else:
            return False
    except (ValueError, tomllib.TOMLDecodeError):
        return False
    return _config_value_at_pointer(value, segments)


def _target_symbol_exists(*, path: Path, content: str, symbol: str) -> bool:
    """Match a concrete definition, binding, constant, or config pointer."""

    if symbol.startswith("config:"):
        return _config_pointer_exists(path=path, content=content, symbol=symbol)
    components = [part for part in re.split(r"[.:#]", symbol) if part]
    if not components:
        return False
    if path.suffix.casefold() == ".py":
        definitions = _python_target_bindings(content)
        module_qualifiers = {path.stem, *path.with_suffix("").parts}
        expected_components = (
            components[-1:]
            if len(components) >= 2 and components[-2] in module_qualifiers
            else components[-2:]
            if len(components) >= 2
            else components
        )
        expected = ".".join(expected_components)
        return expected in definitions or any(
            definition.endswith(f".{expected}") for definition in definitions
        )
    leaf = re.escape(components[-1])
    return any(
        re.search(pattern, content, flags=re.MULTILINE)
        for pattern in (
            rf"\b(?:function|class|interface|struct|enum|def|fn)\s+{leaf}\b",
            rf"\b{leaf}\s*\([^)]*\)\s*(?:\{{|=>)",
            rf"^\s*(?:(?:export|public|private|protected)\s+)?"
            rf"(?:const|let|var|static|readonly)\s+(?:[A-Za-z_$][\w$<>?,.\[\]:]*\s+)?"
            rf"{leaf}\b",
            rf"^\s*(?:export\s+)?{leaf}\s*(?::[^=]+)?=",
        )
    )


def _command_quality_errors(
    command: str,
    *,
    plan_id: str,
    repo_root: Path | None,
    label: str,
) -> list[str]:
    errors: list[str] = []
    safety_errors = verification_command_safety_errors(command)
    if safety_errors:
        errors.extend(
            f"change_plan_unsafe_{label}: {plan_id}: {reason}" for reason in safety_errors
        )
        return errors
    if _VAGUE_COMMAND_RE.search(command) or not _VERIFICATION_TOOL_RE.search(command):
        errors.append(f"change_plan_non_executable_{label}: {plan_id}: {command!r}")
        return errors
    if repo_root is None:
        return errors
    try:
        tokens = shlex.split(command)
    except ValueError:
        return [f"change_plan_invalid_{label}_quoting: {plan_id}"]
    project_root = repo_root
    for index, token in enumerate(tokens[:-1]):
        if token in {"-p", "--project"}:
            candidate = _path_within_repo(repo_root, tokens[index + 1])
            if candidate is None or not candidate.is_dir():
                errors.append(
                    f"change_plan_{label}_project_path_missing: {plan_id}: {tokens[index + 1]!r}"
                )
            else:
                project_root = candidate
    for match in _COMMAND_PATH_RE.finditer(command):
        raw_path = match.group("path").strip("'\"(),;:")
        if raw_path in {"python", "pytest"} or "://" in raw_path:
            continue
        candidate = _path_within_repo(repo_root, raw_path)
        if candidate is None or not candidate.exists():
            project_candidate = _path_within_repo(project_root, raw_path)
            if project_candidate is None or not project_candidate.exists():
                errors.append(f"change_plan_{label}_path_missing: {plan_id}: {raw_path!r}")
    return errors


def change_plan_quality_errors(
    plan: dict[str, Any],
    *,
    expected_revision: str,
    expected_case_id: str | None = None,
    repo_root: Path | None = None,
    problem_record: dict[str, Any] | None = None,
    research_dossier: dict[str, Any] | None = None,
    selection_decision: dict[str, Any] | None = None,
) -> list[str]:
    """Return decision-completeness errors for one stage-6 change plan."""

    plan_id = plan.get("change_plan_id") or "(no change_plan_id)"
    errors: list[str] = []
    if _nonempty_string(plan.get("_parse_warning")):
        errors.append(f"change_plan_stage_contract_invalid: {plan_id}")
    if not _nonempty_string(plan.get("change_plan_id")):
        errors.append("change_plan_missing_stable_id")
    plan_revision_id = plan.get("plan_revision_id")
    if plan_revision_id != plan_revision_id_for(plan):
        errors.append(f"change_plan_invalid_plan_revision_id: {plan_id}")
    if plan.get("plan_revision_source") != "server_content_addressed_v1":
        errors.append(f"change_plan_invalid_plan_revision_source: {plan_id}")
    case_id = plan.get("case_id")
    if not _nonempty_string(case_id):
        errors.append(f"change_plan_missing_case_id: {plan_id}")
    elif expected_case_id is not None and case_id != expected_case_id:
        errors.append(
            f"change_plan_case_id_mismatch: {plan_id}: expected={expected_case_id} got={case_id!r}"
        )
    if plan.get("repo_revision") != expected_revision:
        errors.append(
            f"change_plan_repo_revision_mismatch: {plan_id}: "
            f"expected={expected_revision} got={plan.get('repo_revision')!r}"
        )

    targets = plan.get("change_targets")
    if not isinstance(targets, list) or not targets:
        errors.append(f"change_plan_missing_change_targets: {plan_id}")
    else:
        for index, target in enumerate(targets):
            if not isinstance(target, dict) or not _nonempty_string(target.get("path")):
                errors.append(f"change_plan_invalid_target: {plan_id}: index={index}")
                continue
            path = str(target["path"]).strip()
            action = str(target.get("action") or "").strip()
            if action not in {"modify", "create"}:
                errors.append(f"change_plan_invalid_target_action: {plan_id}: {path}")
            if any(token in path.casefold() for token in ("tbd", "unknown", "*")):
                errors.append(f"change_plan_non_concrete_target: {plan_id}: {path!r}")
            symbols = target.get("symbols")
            if (
                not isinstance(symbols, list)
                or not symbols
                or not all(_nonempty_string(symbol) for symbol in symbols)
            ):
                errors.append(f"change_plan_invalid_target_symbols: {plan_id}: {path}")
            if not _nonempty_string(target.get("change")):
                errors.append(f"change_plan_missing_target_change: {plan_id}: {path}")
            if repo_root is not None:
                target_path = _path_within_repo(repo_root, path)
                if action == "modify" and (target_path is None or not target_path.is_file()):
                    errors.append(f"change_plan_target_path_missing: {plan_id}: {path}")
                elif action == "create" and (
                    target_path is None or target_path.exists()
                ):
                    errors.append(f"change_plan_create_target_invalid: {plan_id}: {path}")
                elif action == "modify" and isinstance(symbols, list):
                    target_text = target_path.read_text(encoding="utf-8", errors="replace")
                    for symbol in symbols:
                        if not _nonempty_string(symbol):
                            continue
                        if not _target_symbol_exists(
                            path=target_path,
                            content=target_text,
                            symbol=str(symbol).strip(),
                        ):
                            errors.append(
                                f"change_plan_target_symbol_missing: {plan_id}: {path}:{symbol}"
                            )

    implementation_steps = plan.get("implementation_steps")
    if not _string_list(implementation_steps, allow_empty=False):
        errors.append(f"change_plan_invalid_implementation_steps: {plan_id}")
    else:
        for step in implementation_steps:
            if _nonempty_string(step) and _DISCOVERY_FIRST_RE.search(str(step)):
                errors.append(f"change_plan_discovery_first_step: {plan_id}: {step}")

    if not _string_list(plan.get("verification_steps"), allow_empty=False):
        errors.append(f"change_plan_invalid_verification_steps: {plan_id}")
    if not _string_list(plan.get("success_criteria"), allow_empty=False):
        errors.append(f"change_plan_invalid_success_criteria: {plan_id}")

    commands = plan.get("verification_commands")
    if not _string_list(commands, allow_empty=False):
        errors.append(f"change_plan_missing_verification_commands: {plan_id}")
    else:
        for command in commands:
            errors.extend(
                _command_quality_errors(
                    command,
                    plan_id=str(plan_id),
                    repo_root=repo_root,
                    label="verification_command",
                )
            )

    outcome_roles = plan.get("outcome_verification_roles")
    if not isinstance(outcome_roles, dict):
        errors.append(f"change_plan_missing_outcome_verification_roles: {plan_id}")
    else:
        for role, role_contract in outcome_roles.items():
            if role_contract is None:
                continue
            if not isinstance(role_contract, dict):
                errors.append(f"change_plan_invalid_outcome_role: {plan_id}: {role}")
                continue
            role_commands = role_contract.get("commands")
            if not _string_list(role_commands, allow_empty=False):
                errors.append(f"change_plan_missing_outcome_role_commands: {plan_id}: {role}")
                continue
            for command in role_commands:
                errors.extend(
                    _command_quality_errors(
                        command,
                        plan_id=str(plan_id),
                        repo_root=None,
                        label=f"outcome_role_{role}",
                    )
                )

    reproduction = plan.get("before_after_reproduction")
    if not isinstance(reproduction, dict):
        errors.append(f"change_plan_missing_before_after_reproduction: {plan_id}")
    else:
        if not _nonempty_string(reproduction.get("original_scenario")):
            errors.append(f"change_plan_missing_original_scenario: {plan_id}")
        limitation = reproduction.get("proof_limitation")
        before = reproduction.get("before_change")
        after = reproduction.get("after_change")
        if _nonempty_string(limitation):
            errors.append(f"change_plan_unverified_proof_limitation: {plan_id}")
            alternate = reproduction.get("alternate_verification")
            if not _nonempty_string(alternate):
                errors.append(f"change_plan_missing_alternate_verification: {plan_id}")
            else:
                errors.extend(
                    _command_quality_errors(
                        str(alternate),
                        plan_id=str(plan_id),
                        repo_root=repo_root,
                        label="alternate_verification",
                    )
                )
                commands_raw = plan.get("verification_commands")
                commands = commands_raw if isinstance(commands_raw, list) else []
                normalized_alternate = " ".join(str(alternate).split())
                if normalized_alternate not in {
                    " ".join(str(command).split())
                    for command in commands
                    if isinstance(command, str)
                }:
                    errors.append(
                        f"change_plan_alternate_not_in_verification_commands: {plan_id}"
                    )
            limitation_refs_raw = reproduction.get("proof_limitation_refs")
            limitation_refs = (
                [ref.strip() for ref in limitation_refs_raw if isinstance(ref, str) and ref.strip()]
                if isinstance(limitation_refs_raw, list)
                else []
            )
            allowed_limitations = research_limitation_references(research_dossier)
            if (
                not limitation_refs
                or len(limitation_refs) != len(limitation_refs_raw)
                or any(ref not in allowed_limitations for ref in limitation_refs)
            ):
                errors.append(f"change_plan_unbound_proof_limitation: {plan_id}")
        else:
            if reproduction.get("proof_limitation_refs") not in (None, []):
                errors.append(f"change_plan_limitation_refs_without_limitation: {plan_id}")
            for phase, mapping in (("before", before), ("after", after)):
                if not isinstance(mapping, dict):
                    errors.append(f"change_plan_missing_{phase}_mapping: {plan_id}")
                    continue
                if not _nonempty_string(mapping.get("command")):
                    errors.append(f"change_plan_missing_{phase}_command: {plan_id}")
                else:
                    errors.extend(
                        _command_quality_errors(
                            str(mapping.get("command")),
                            plan_id=str(plan_id),
                            repo_root=repo_root,
                            label=f"{phase}_command",
                        )
                    )
                if not _nonempty_string(mapping.get("expected_result")):
                    errors.append(f"change_plan_missing_{phase}_expected_result: {plan_id}")

    compatibility = plan.get("compatibility_and_failure_modes")
    if not isinstance(compatibility, dict):
        errors.append(f"change_plan_missing_compatibility_and_failure_modes: {plan_id}")
    else:
        if not _string_list(compatibility.get("preserved_behaviors"), allow_empty=False):
            errors.append(f"change_plan_missing_preserved_behaviors: {plan_id}")
        if not _string_list(compatibility.get("intentional_changes"), allow_empty=True):
            errors.append(f"change_plan_invalid_intentional_changes: {plan_id}")
        if not _string_list(compatibility.get("failure_modes"), allow_empty=False):
            errors.append(f"change_plan_missing_failure_modes: {plan_id}")
        if not isinstance(compatibility.get("migration_required"), bool):
            errors.append(f"change_plan_missing_migration_decision: {plan_id}")

    if not isinstance(plan.get("causal_coverage"), dict):
        errors.append(f"change_plan_missing_causal_coverage: {plan_id}")
    if not isinstance(plan.get("scope_evidence"), dict):
        errors.append(f"change_plan_missing_scope_evidence: {plan_id}")
    if not isinstance(plan.get("requires_live_verification"), bool):
        errors.append(f"change_plan_missing_requires_live_verification: {plan_id}")
    if not _nonempty_string(plan.get("live_verification_rationale")):
        errors.append(f"change_plan_missing_live_verification_rationale: {plan_id}")
    if (
        problem_record is not None
        and research_dossier is not None
        and selection_decision is not None
    ):
        authoritative_ready, authoritative_reasons = assess_change_plan_readiness(
            plan,
            problem=problem_record,
            research=research_dossier,
            selection=selection_decision,
        )
        if not authoritative_ready:
            errors.extend(authoritative_reasons)
    return list(dict.fromkeys(errors))


__all__ = [
    "change_plan_quality_errors",
    "assess_repo_grounding",
    "falsification_review_errors",
    "parse_optioning_response",
    "read_only_stage_tools",
    "read_repo_revision",
    "repo_contains_revision",
    "repo_context_payload",
    "selection_quality_errors",
    "stage_include_directories",
]
