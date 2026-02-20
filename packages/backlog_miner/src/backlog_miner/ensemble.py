from __future__ import annotations

import json
import os
import random
import tempfile
import warnings
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent_adapters import run_claude_print, run_codex_exec, run_gemini
from backlog_core import (
    build_merge_candidates,
    compute_backlog_coverage,
    dedupe_tickets,
    parse_ticket_list,
)
from runner_core import RunnerConfig

_PROMPT_MANIFEST_FILENAME = "manifest.json"

_CODEX_HOST_LOGIN_BLOCKED_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_ORG_ID",
)


@dataclass(frozen=True)
class MinerJob:
    tag: str
    template_name: str
    atoms: list[dict[str, Any]]
    pass_type: str
    selection_params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptManifest:
    coverage_templates: tuple[str, ...]
    bagging_templates: tuple[str, ...]
    orphan_template: str
    merge_judge_template: str
    labeler_template: str


_CHANGE_SURFACE_KIND_ENUM = {
    "new_command",
    "new_flag",
    "docs_change",
    "behavior_change",
    "breaking_change",
    "new_top_level_mode",
    "new_config_schema",
    "new_api",
    "unknown",
}

_LABELER_COMPONENT_ENUM = {
    "docs",
    "runner_core",
    "sandbox_runner",
    "agent_adapters",
    "config",
    "unknown",
}

_LABELER_INTENT_RISK_ENUM = {"low", "med", "high"}


def _warn_nonfatal_fallback(*, code: str, message: str) -> None:
    """Emit a visible warning when best-effort fallback behavior is used.

    Parameters
    ----------
    code:
        Stable machine-readable fallback code.
    message:
        Human-readable explanation with remediation context.
    """

    warnings.warn(f"[{code}] {message}", RuntimeWarning, stacklevel=2)


def _coerce_string(value: Any) -> str | None:
    """Normalize a potential string into a trimmed value.

    Parameters
    ----------
    value:
        Candidate value to normalize.

    Returns
    -------
    str | None
        Trimmed non-empty string when coercion succeeds, otherwise ``None``.
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_float_01(value: Any) -> float:
    """Coerce confidence-like values into the inclusive range ``[0.0, 1.0]``.

    Parameters
    ----------
    value:
        Candidate numeric/string value.

    Returns
    -------
    float
        Clamped float value in ``[0.0, 1.0]``.
    """

    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        try:
            return max(0.0, min(1.0, float(value.strip())))
        except ValueError:
            _warn_nonfatal_fallback(
                code="invalid_confidence_string",
                message=(
                    "Received non-numeric confidence string from labeler output; "
                    "coercing to 0.0."
                ),
            )
            return 0.0
    _warn_nonfatal_fallback(
        code="invalid_confidence_type",
        message=(
            "Received non-numeric confidence value from labeler output; coercing to 0.0."
        ),
    )
    return 0.0


def _ticket_anchor(ticket: dict[str, Any]) -> str:
    """Build deterministic ticket anchor used for dedupe/fingerprints.

    Parameters
    ----------
    ticket:
        Ticket payload.

    Returns
    -------
    str
        JSON-serialized anchor based on normalized title and evidence IDs.
    """

    title = _coerce_string(ticket.get("title")) or ""
    evidence = sorted(
        item for item in ticket.get("evidence_atom_ids", []) if isinstance(item, str)
    )
    return json.dumps({"title": title.lower(), "evidence": evidence}, ensure_ascii=False)


def _tickets_match_atom_scope(
    tickets: list[dict[str, Any]],
    *,
    allowed_atom_ids: set[str],
) -> bool:
    """Validate that cached ticket evidence remains inside current atom scope.

    Parameters
    ----------
    tickets:
        Cached ticket payload list.
    allowed_atom_ids:
        Atom IDs allowed for current miner input manifest.

    Returns
    -------
    bool
        ``True`` when every evidence atom ID stays in scope, else ``False``.
    """

    for ticket in tickets:
        evidence_ids = [
            item
            for item in ticket.get("evidence_atom_ids", [])
            if isinstance(item, str)
        ]
        if any(atom_id not in allowed_atom_ids for atom_id in evidence_ids):
            return False
    return True


def _read_text(path: Path) -> str:
    """Read UTF-8 text when file exists.

    Parameters
    ----------
    path:
        Candidate file path.

    Returns
    -------
    str
        File contents, or an empty string when the file does not exist.
    """

    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON payload with stable formatting.

    Parameters
    ----------
    path:
        Destination JSON path.
    payload:
        Serializable payload.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    """Load a JSON object from disk.

    Parameters
    ----------
    path:
        JSON file path.

    Returns
    -------
    dict[str, Any] | None
        Parsed mapping when file exists, otherwise ``None``.

    Raises
    ------
    ValueError
        Raised when file content exists but is invalid JSON or not a JSON object.
    """

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON object at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {path}, got {type(payload).__name__}.")
    return payload


def _json_digest(payload: Any) -> str:
    """Compute stable digest for JSON-like payload.

    Parameters
    ----------
    payload:
        JSON-serializable value.

    Returns
    -------
    str
        SHA-256 hex digest over sorted compact JSON serialization.
    """

    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(stable.encode("utf-8")).hexdigest()




def _sha256_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return sha256(data).hexdigest()


def _atoms_content_digest(atoms: list[dict[str, Any]]) -> str:
    """Digest atoms by (atom_id, text_hash) in *prompt order*.

    This is intentionally order-sensitive because prompt ordering can change model output.
    """

    pairs: list[list[str]] = []
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        atom_id = _coerce_string(atom.get("atom_id")) or ""
        text = atom.get("text")
        text_s = text if isinstance(text, str) else ""
        pairs.append([atom_id, sha256(text_s.encode("utf-8")).hexdigest()])
    stable = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return sha256(stable.encode("utf-8")).hexdigest()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(str(raw).strip())
    except ValueError:
        _warn_nonfatal_fallback(
            code="invalid_env_int",
            message=f"Environment variable {name}={raw!r} is not an int; using default {default}.",
        )
        return int(default)


def _select_evidence_atoms(
    atoms_by_id: dict[str, dict[str, Any]],
    evidence_ids: list[str],
    *,
    max_atoms: int,
    max_total_chars: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Select evidence atoms with both count + total-text guards.

    Returns (atoms, atom_ids) in the same order as evidence_ids (after filtering).
    """

    selected_atoms: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    total_chars = 0
    max_atoms = max(0, int(max_atoms))
    max_total_chars = max(0, int(max_total_chars))

    for atom_id in evidence_ids:
        atom = atoms_by_id.get(atom_id)
        if not isinstance(atom, dict):
            continue
        text = atom.get("text")
        text_s = text if isinstance(text, str) else ""
        candidate_chars = len(text_s)

        if max_atoms and len(selected_atoms) >= max_atoms:
            break
        if max_total_chars and selected_atoms and (total_chars + candidate_chars) > max_total_chars:
            break

        selected_atoms.append(atom)
        selected_ids.append(atom_id)
        total_chars += candidate_chars

    return selected_atoms, selected_ids

def _manifest_string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    """Validate manifest field as a non-empty list of template names.

    Parameters
    ----------
    value:
        Raw field value from parsed manifest.
    field_name:
        Fully-qualified field label for error messages.

    Returns
    -------
    tuple[str, ...]
        Normalized non-empty template-name tuple.
    """

    if not isinstance(value, list):
        raise ValueError(f"Prompt manifest field `{field_name}` must be a list of template names.")
    out: list[str] = []
    for item in value:
        item_s = _coerce_string(item)
        if item_s is None:
            raise ValueError(
                f"Prompt manifest field `{field_name}` contains a non-string template name."
            )
        out.append(item_s)
    if not out:
        raise ValueError(f"Prompt manifest field `{field_name}` must not be empty.")
    return tuple(out)


def _manifest_string(value: Any, *, field_name: str) -> str:
    """Validate manifest field as a single template filename.

    Parameters
    ----------
    value:
        Raw field value from parsed manifest.
    field_name:
        Fully-qualified field label for error messages.

    Returns
    -------
    str
        Normalized non-empty template filename.
    """

    text = _coerce_string(value)
    if text is None:
        raise ValueError(f"Prompt manifest field `{field_name}` must be a template filename.")
    return text


def _ensure_prompt_template_exists(prompts_dir: Path, template_name: str) -> None:
    """Ensure prompt template file exists before use.

    Parameters
    ----------
    prompts_dir:
        Prompt directory root.
    template_name:
        Template filename referenced by manifest.
    """

    path = prompts_dir / template_name
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(
            "Missing prompt template "
            f"`{template_name}` in prompts directory `{prompts_dir}`."
        )


def load_prompt_manifest(prompts_dir: Path) -> PromptManifest:
    """Load and validate backlog miner prompt manifest.

    Parameters
    ----------
    prompts_dir:
        Directory expected to contain `manifest.json` plus template files.

    Returns
    -------
    PromptManifest
        Parsed and validated manifest object.
    """

    manifest_path = prompts_dir / _PROMPT_MANIFEST_FILENAME
    if not prompts_dir.exists() or not prompts_dir.is_dir():
        raise FileNotFoundError(f"Prompt directory not found: {prompts_dir}")
    if not manifest_path.exists():
        raise FileNotFoundError(
            "Missing backlog prompt manifest: "
            f"`{manifest_path}` (expected `{_PROMPT_MANIFEST_FILENAME}` in prompts dir)."
        )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Prompt manifest must be a JSON object: {manifest_path}")

    version = payload.get("version")
    if version != 1:
        raise ValueError(f"Unsupported prompt manifest version `{version}` in {manifest_path}")

    miners_raw = payload.get("miners")
    if not isinstance(miners_raw, dict):
        raise ValueError(f"Prompt manifest missing `miners` mapping: {manifest_path}")

    coverage_templates = _manifest_string_list(
        miners_raw.get("coverage_templates"),
        field_name="miners.coverage_templates",
    )
    bagging_templates = _manifest_string_list(
        miners_raw.get("bagging_templates"),
        field_name="miners.bagging_templates",
    )
    orphan_template = _manifest_string(
        miners_raw.get("orphan_template"),
        field_name="miners.orphan_template",
    )
    merge_template = _manifest_string(
        payload.get("merge_judge_template"),
        field_name="merge_judge_template",
    )
    labeler_template = _manifest_string(
        payload.get("labeler_template"),
        field_name="labeler_template",
    )

    for template_name in {
        *coverage_templates,
        *bagging_templates,
        orphan_template,
        merge_template,
        labeler_template,
    }:
        _ensure_prompt_template_exists(prompts_dir, template_name)

    return PromptManifest(
        coverage_templates=coverage_templates,
        bagging_templates=bagging_templates,
        orphan_template=orphan_template,
        merge_judge_template=merge_template,
        labeler_template=labeler_template,
    )


def _load_prompt_template(prompts_dir: Path, template_name: str) -> str:
    """Load prompt template text by name.

    Parameters
    ----------
    prompts_dir:
        Prompt directory.
    template_name:
        Template filename from manifest.

    Returns
    -------
    str
        Template content as UTF-8 text.
    """

    _ensure_prompt_template_exists(prompts_dir, template_name)
    path = prompts_dir / template_name
    return path.read_text(encoding="utf-8")


def _render_template(template: str, replacements: dict[str, str]) -> str:
    """Render string template via literal ``{{KEY}}`` replacement.

    Parameters
    ----------
    template:
        Raw template string.
    replacements:
        Mapping of placeholder keys to replacement text.

    Returns
    -------
    str
        Rendered template.
    """

    out = template
    for key, value in replacements.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def _ticket_fingerprint(ticket: dict[str, Any]) -> str:
    """
    Produce a stable filesystem-safe fingerprint for caching per-ticket artifacts.

    This fingerprint is intentionally derived only from a normalized ticket anchor
    (title + evidence ids) so that resume runs can reuse cached labeler outputs.
    """

    anchor = _ticket_anchor(ticket)
    return sha256(anchor.encode("utf-8")).hexdigest()[:16]


def _parse_first_json_object(raw_text: str) -> dict[str, Any] | None:
    """Parse the first JSON object embedded in free-form text.

    Parameters
    ----------
    raw_text:
        Agent raw output text.

    Returns
    -------
    dict[str, Any] | None
        First decoded JSON object, or ``None`` when no object can be parsed.
    """

    text = raw_text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        parsed = None

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    _warn_nonfatal_fallback(
        code="json_object_missing",
        message="Agent output did not contain a parsable JSON object.",
    )
    return None


def _normalize_labeler_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize labeler payload into validated structured schema.

    Parameters
    ----------
    payload:
        Raw labeler JSON object.

    Returns
    -------
    dict[str, Any]
        Normalized payload compatible with backlog policy downstream.
    """

    change_surface_raw = payload.get("change_surface")
    change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
    raw_kinds = change_surface.get("kinds")
    kinds = [
        kind
        for kind in (raw_kinds if isinstance(raw_kinds, list) else [])
        if isinstance(kind, str) and kind.strip()
    ]
    kinds_norm = [kind.strip() for kind in kinds if kind.strip() in _CHANGE_SURFACE_KIND_ENUM]
    if not kinds_norm:
        _warn_nonfatal_fallback(
            code="labeler_unknown_change_surface",
            message=(
                "Labeler output omitted valid change-surface kinds; defaulting to "
                "`unknown`."
            ),
        )
        kinds_norm = ["unknown"]

    component = _coerce_string(payload.get("component")) or "unknown"
    if component not in _LABELER_COMPONENT_ENUM:
        _warn_nonfatal_fallback(
            code="labeler_unknown_component",
            message="Labeler output component was unsupported; defaulting to `unknown`.",
        )
        component = "unknown"

    intent_risk = _coerce_string(payload.get("intent_risk")) or "med"
    if intent_risk not in _LABELER_INTENT_RISK_ENUM:
        _warn_nonfatal_fallback(
            code="labeler_unknown_intent_risk",
            message="Labeler output intent_risk was unsupported; defaulting to `med`.",
        )
        intent_risk = "med"

    raw_evidence = payload.get("evidence_atom_ids_used")
    evidence_used = [
        item.strip()
        for item in (raw_evidence if isinstance(raw_evidence, list) else [])
        if isinstance(item, str) and item.strip()
    ]

    return {
        "change_surface": {
            "user_visible": bool(change_surface.get("user_visible")),
            "kinds": sorted(set(kinds_norm)),
            "notes": _coerce_string(change_surface.get("notes")) or "",
        },
        "component": component,
        "intent_risk": intent_risk,
        "confidence": _coerce_float_01(payload.get("confidence")),
        "evidence_atom_ids_used": evidence_used,
    }


def _majority_vote_str(values: list[str], *, default: str) -> str:
    """Return strict-majority string vote or a configured default.

    Parameters
    ----------
    values:
        Candidate categorical string values.
    default:
        Value returned when no strict majority exists.

    Returns
    -------
    str
        Majority value or ``default``.
    """

    cleaned = [v for v in values if isinstance(v, str) and v]
    if not cleaned:
        return default
    counts = Counter(cleaned)
    top, top_count = counts.most_common(1)[0]
    if top_count >= (len(cleaned) // 2) + 1:
        return top
    return default


def _majority_vote_bool(values: list[bool], *, default: bool) -> bool:
    """Return strict-majority boolean vote or default on tie/empty.

    Parameters
    ----------
    values:
        Candidate boolean values.
    default:
        Value returned when votes are tied or empty.

    Returns
    -------
    bool
        Majority boolean value or ``default``.
    """

    if not values:
        return default
    true_count = sum(1 for v in values if v)
    false_count = len(values) - true_count
    if true_count == false_count:
        return default
    return true_count > false_count


def _consensus_labeler_payload(payloads: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    """Build consensus payload and disagreement flag from labeler variants.

    Parameters
    ----------
    payloads:
        Normalized labeler payloads from parallel variants.

    Returns
    -------
    tuple[dict[str, Any], bool]
        Consensus payload and disagreement boolean.
    """

    if not payloads:
        return (
            {
                "change_surface": {"user_visible": False, "kinds": ["unknown"], "notes": ""},
                "component": "unknown",
                "intent_risk": "med",
                "confidence": 0.0,
                "evidence_atom_ids_used": [],
            },
            True,
        )

    variants = [
        _normalize_labeler_payload(payload)
        for payload in payloads
        if isinstance(payload, dict)
    ]
    if not variants:
        return (
            {
                "change_surface": {"user_visible": False, "kinds": ["unknown"], "notes": ""},
                "component": "unknown",
                "intent_risk": "med",
                "confidence": 0.0,
                "evidence_atom_ids_used": [],
            },
            True,
        )

    majority = (len(variants) // 2) + 1
    kind_votes: Counter[str] = Counter()
    for variant in variants:
        kinds = variant.get("change_surface", {}).get("kinds", [])
        kinds_list = [k for k in kinds if isinstance(k, str) and k in _CHANGE_SURFACE_KIND_ENUM]
        for kind in set(kinds_list):
            kind_votes[kind] += 1

    consensus_kinds = sorted([k for k, c in kind_votes.items() if c >= majority and k != "unknown"])
    if not consensus_kinds:
        consensus_kinds = ["unknown"]

    user_visible_values = [
        bool(variant.get("change_surface", {}).get("user_visible")) for variant in variants
    ]
    consensus_user_visible = _majority_vote_bool(user_visible_values, default=False)

    component_values = [
        _coerce_string(variant.get("component")) or "" for variant in variants
    ]
    consensus_component = _majority_vote_str(component_values, default="unknown")
    if consensus_component not in _LABELER_COMPONENT_ENUM:
        consensus_component = "unknown"

    intent_values = [_coerce_string(variant.get("intent_risk")) or "" for variant in variants]
    consensus_intent = _majority_vote_str(intent_values, default="med")
    if consensus_intent not in _LABELER_INTENT_RISK_ENUM:
        consensus_intent = "med"

    confidence_values = [_coerce_float_01(variant.get("confidence")) for variant in variants]
    confidence_avg = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0

    evidence_used: list[str] = []
    for variant in variants:
        evidence_used.extend(
            [
                item
                for item in variant.get("evidence_atom_ids_used", [])
                if isinstance(item, str) and item.strip()
            ]
        )
    evidence_used_deduped: list[str] = []
    seen: set[str] = set()
    for item in evidence_used:
        if item in seen:
            continue
        evidence_used_deduped.append(item)
        seen.add(item)

    disagreement = False
    if len({tuple(v.get("change_surface", {}).get("kinds", [])) for v in variants}) > 1:
        disagreement = True
    if len({bool(v.get("change_surface", {}).get("user_visible")) for v in variants}) > 1:
        disagreement = True
    if len({v.get("component") for v in variants}) > 1:
        disagreement = True

    notes = ""
    for variant in variants:
        candidate = _coerce_string(variant.get("change_surface", {}).get("notes"))
        if candidate:
            notes = candidate
            break

    return (
        {
            "change_surface": {
                "user_visible": consensus_user_visible,
                "kinds": consensus_kinds,
                "notes": notes,
            },
            "component": consensus_component,
            "intent_risk": consensus_intent,
            "confidence": max(0.0, min(1.0, confidence_avg)),
            "evidence_atom_ids_used": evidence_used_deduped[: _env_int("BACKLOG_LABELER_MAX_EVIDENCE_IDS_USED", 32)],
        },
        disagreement,
    )


def run_labeler_jobs(
    *,
    tickets: list[dict[str, Any]],
    atoms_by_id: dict[str, dict[str, Any]],
    prompts_dir: Path,
    prompt_manifest: PromptManifest,
    artifacts_dir: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    labelers: int,
    resume: bool,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """
    Run an ensemble of ticket labelers and patch tickets with structured change-surface fields.

    This is an optional stage intended to be run after mining/deduping, before applying
    policy or rendering/writing backlog artifacts.
    """

    labelers = max(0, int(labelers))
    if labelers <= 0 or not tickets:
        return {
            "tickets": tickets,
            "labelers_meta": {
                "labelers_total": labelers,
                "tickets_total": len(tickets),
            },
        }

    template = _load_prompt_template(prompts_dir, prompt_manifest.labeler_template)

    template_sha256 = _sha256_text(template)
    cfg_sha256 = _json_digest({"agent": cfg.agents.get(agent, {}), "policies": cfg.policies})
    max_evidence_atoms = _env_int("BACKLOG_LABELER_MAX_EVIDENCE_ATOMS", 25)
    max_evidence_chars = _env_int("BACKLOG_LABELER_MAX_EVIDENCE_CHARS", 60000)

    labeler_root = artifacts_dir / "labeler"
    labeler_root.mkdir(parents=True, exist_ok=True)

    variants = ["conservative", "balanced", "skeptical"]
    labeled: list[dict[str, Any]] = []
    per_ticket_meta: list[dict[str, Any]] = []
    cached_runs = 0
    labeler_runs = 0
    parse_failed = 0

    for ticket in tickets:
        fingerprint = _ticket_fingerprint(ticket)
        ticket_dir = labeler_root / fingerprint
        ticket_dir.mkdir(parents=True, exist_ok=True)

        raw_evidence_ids = ticket.get("evidence_atom_ids", [])
        evidence_ids = [item for item in raw_evidence_ids if isinstance(item, str)]
        evidence_atoms, evidence_ids_included = _select_evidence_atoms(
            atoms_by_id,
            evidence_ids,
            max_atoms=max_evidence_atoms,
            max_total_chars=max_evidence_chars,
        )
        evidence_payload: list[dict[str, Any]] = []
        for atom in evidence_atoms:
            if not isinstance(atom, dict):
                continue
            evidence_payload.append(
                {
                    "atom_id": atom.get("atom_id"),
                    "run_rel": atom.get("run_rel"),
                    "target_slug": atom.get("target_slug"),
                    "repo_input": atom.get("repo_input"),
                    "mission_id": atom.get("mission_id"),
                    "persona_id": atom.get("persona_id"),
                    "agent": atom.get("agent"),
                    "source": atom.get("source"),
                    "severity_hint": atom.get("severity_hint"),
                    "text": atom.get("text"),
                    "failure_kind": atom.get("failure_kind"),
                    "error": atom.get("error"),
                    "report_validation_errors": atom.get("report_validation_errors"),
                    "artifacts": atom.get("artifacts"),
                    "attachments": atom.get("attachments"),
                }
            )

        ticket_payload = {
            "title": ticket.get("title"),
            "problem": ticket.get("problem"),
            "user_impact": ticket.get("user_impact"),
            "severity": ticket.get("severity"),
            "confidence": ticket.get("confidence"),
            "evidence_atom_ids": evidence_ids,
            "proposed_fix": ticket.get("proposed_fix"),
            "investigation_steps": ticket.get("investigation_steps"),
            "success_criteria": ticket.get("success_criteria"),
        }

        payloads: list[dict[str, Any]] = []
        run_statuses: list[str] = []
        for idx in range(1, labelers + 1):
            variant = variants[(idx - 1) % len(variants)]
            prompt = _render_template(
                template,
                {
                    "LABELER_VARIANT": variant,
                    "TICKET_JSON": json.dumps(ticket_payload, indent=2, ensure_ascii=False),
                    "EVIDENCE_ATOMS_JSON": json.dumps(
                        evidence_payload, indent=2, ensure_ascii=False
                    ),
                },
            )

            tag = f"labeler_{idx:02d}"
            cached_path = ticket_dir / f"{tag}.label.json"
            manifest_path = ticket_dir / f"{tag}.input.json"

            input_manifest = {
                "version": 1,
                "template": prompt_manifest.labeler_template,
                "template_sha256": template_sha256,
                "agent": agent,
                "model": model or "",
                "variant": variant,
                "max_evidence_atoms": max_evidence_atoms,
                "max_evidence_chars": max_evidence_chars,
                "ticket_anchor": _ticket_anchor(ticket),
                "ticket_payload_sha256": _json_digest(ticket_payload),
                "evidence_atom_ids_total": evidence_ids,
                "evidence_atom_ids_included": evidence_ids_included,
                "evidence_atoms_sha256": _atoms_content_digest(evidence_atoms),
                "cfg_sha256": cfg_sha256,
            }
            input_manifest_digest = _json_digest(input_manifest)

            if resume and not force and cached_path.exists():
                cached_manifest: dict[str, Any] | None = None
                if manifest_path.exists():
                    try:
                        cached_manifest = _load_json_dict(manifest_path)
                    except ValueError:
                        _warn_nonfatal_fallback(
                            code="labeler_cache_manifest_invalid",
                            message=(
                                f"Cached labeler manifest was invalid JSON ({manifest_path}); "
                                "using cached output without manifest validation."
                            ),
                        )
                        cached_manifest = None

                if (
                    isinstance(cached_manifest, dict)
                    and _json_digest(cached_manifest) == input_manifest_digest
                ):
                    try:
                        cached = json.loads(cached_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        _warn_nonfatal_fallback(
                            code="labeler_cache_json_invalid",
                            message=(
                                f"Cached labeler output was invalid JSON ({cached_path}); "
                                "rerunning labeler for this ticket."
                            ),
                        )
                        cached = None
                    if isinstance(cached, dict):
                        payloads.append(cached)
                        cached_runs += 1
                        run_statuses.append("cached")
                        continue
                elif cached_manifest is None:
                    # Legacy cache: output exists but no (or invalid) manifest. Use the cached output
                    # (best-effort) and upgrade by writing a manifest for future resume runs.
                    try:
                        cached = json.loads(cached_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        cached = None
                    if isinstance(cached, dict):
                        payloads.append(cached)
                        cached_runs += 1
                        run_statuses.append("cached")
                        _write_json(manifest_path, input_manifest)
                        continue

            if dry_run:
                (ticket_dir / f"{tag}.dry_run.prompt.txt").write_text(prompt, encoding="utf-8")
                run_statuses.append("dry_run")
                continue

            labeler_runs += 1
            raw_text = run_backlog_prompt(
                agent=agent,
                prompt=prompt,
                out_dir=ticket_dir,
                tag=tag,
                model=model,
                cfg=cfg,
            )
            parsed = _parse_first_json_object(raw_text)
            if not isinstance(parsed, dict):
                parse_failed += 1
                (ticket_dir / f"{tag}.parse_error.txt").write_text(
                    raw_text.strip() + "\n",
                    encoding="utf-8",
                )
                run_statuses.append("parse_failed")
                continue

            normalized = _normalize_labeler_payload(parsed)
            _write_json(cached_path, normalized)
            _write_json(manifest_path, input_manifest)
            payloads.append(normalized)
            run_statuses.append("ok")

        consensus, disagreement = _consensus_labeler_payload(payloads)
        patched = dict(ticket)
        patched["change_surface"] = consensus.get("change_surface", {})
        patched["component"] = consensus.get("component", "unknown")
        patched["intent_risk"] = consensus.get("intent_risk", "med")
        patched["labeler_confidence"] = consensus.get("confidence", 0.0)
        patched["labeler_evidence_atom_ids_used"] = consensus.get("evidence_atom_ids_used", [])

        if disagreement and (_coerce_string(patched.get("stage")) or "triage") == "triage":
            patched["stage"] = "research_required"
            existing = patched.get("risks")
            risks = (
                [item for item in existing if isinstance(item, str) and item.strip()]
                if isinstance(existing, list)
                else []
            )
            if "intent_mismatch_risk" not in risks:
                risks.append("intent_mismatch_risk")
            patched["risks"] = risks

        labeled.append(patched)
        per_ticket_meta.append(
            {
                "fingerprint": fingerprint,
                "labelers": labelers,
                "valid_labels": len(payloads),
                "disagreement": disagreement,
                "statuses": run_statuses,
            }
        )

    meta = {
        "labelers_total": labelers,
        "labeler_template": prompt_manifest.labeler_template,
        "labeler_template_sha256": template_sha256,
        "max_evidence_atoms": max_evidence_atoms,
        "max_evidence_chars": max_evidence_chars,
        "cfg_sha256": cfg_sha256,
        "tickets_total": len(tickets),
        "cached_runs": cached_runs,
        "labeler_runs": labeler_runs,
        "parse_failed": parse_failed,
        "tickets_meta": per_ticket_meta,
    }
    _write_json(labeler_root / "meta.json", meta)
    return {"tickets": labeled, "labelers_meta": meta}


def _agent_binary(cfg: RunnerConfig, agent: str, default: str) -> str:
    """Resolve agent binary from runner config with explicit fallback warning.

    Parameters
    ----------
    cfg:
        Runner configuration.
    agent:
        Agent identifier.
    default:
        Default binary when config omits an explicit path.

    Returns
    -------
    str
        Binary command to execute.
    """

    agents_cfg = cfg.agents if isinstance(cfg.agents, dict) else {}
    raw = agents_cfg.get(agent)
    if isinstance(raw, dict):
        binary = raw.get("binary")
        if isinstance(binary, str) and binary.strip():
            return binary.strip()
    _warn_nonfatal_fallback(
        code="agent_binary_defaulted",
        message=(
            f"No binary configured for agent={agent!r}; defaulting to {default!r}. "
            "Set configs/agents.yaml to make this explicit."
        ),
    )
    return default


def _agent_output_format(cfg: RunnerConfig, agent: str, default: str = "stream-json") -> str:
    """Resolve agent output format with explicit fallback warning.

    Parameters
    ----------
    cfg:
        Runner configuration.
    agent:
        Agent identifier.
    default:
        Output format used when config omits explicit format.

    Returns
    -------
    str
        Output format consumed by adapter normalization.
    """

    agents_cfg = cfg.agents if isinstance(cfg.agents, dict) else {}
    raw = agents_cfg.get(agent)
    if isinstance(raw, dict):
        value = raw.get("output_format")
        if isinstance(value, str) and value.strip():
            return value.strip()
    _warn_nonfatal_fallback(
        code="agent_output_format_defaulted",
        message=(
            f"No output_format configured for agent={agent!r}; defaulting to "
            f"{default!r}."
        ),
    )
    return default


def _codex_overrides(cfg: RunnerConfig) -> list[str]:
    """Resolve configured Codex override strings.

    Parameters
    ----------
    cfg:
        Runner configuration.

    Returns
    -------
    list[str]
        Non-empty override strings from `agents.codex.config_overrides`.
    """

    agents_cfg = cfg.agents if isinstance(cfg.agents, dict) else {}
    raw = agents_cfg.get("codex")
    if isinstance(raw, dict):
        overrides = raw.get("config_overrides")
        if isinstance(overrides, list):
            return [item for item in overrides if isinstance(item, str) and item.strip()]
    return []


@contextmanager
def _codex_host_login_env() -> Any:
    """
    Prefer host login state (~/.codex) over API-key env auth for Codex backlog mining.
    """
    previous: dict[str, str] = {}
    for key in _CODEX_HOST_LOGIN_BLOCKED_ENV_VARS:
        if key in os.environ:
            previous[key] = os.environ.pop(key)
    try:
        yield
    finally:
        for key in _CODEX_HOST_LOGIN_BLOCKED_ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update(previous)


def run_backlog_prompt(
    *,
    agent: str,
    prompt: str,
    out_dir: Path,
    tag: str,
    model: str | None,
    cfg: RunnerConfig,
) -> str:
    """Execute a single backlog prompt and persist raw agent artifacts.

    Parameters
    ----------
    agent:
        Agent identifier.
    prompt:
        Rendered prompt text.
    out_dir:
        Output artifact directory.
    tag:
        Artifact filename tag.
    model:
        Optional model override.
    cfg:
        Runner configuration.

    Returns
    -------
    str
        Assistant output text extracted from adapter events.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{tag}.prompt.txt"
    raw_events_path = out_dir / f"{tag}.raw_events.jsonl"
    last_message_path = out_dir / f"{tag}.last_message.txt"
    stderr_path = out_dir / f"{tag}.stderr.txt"

    prompt_path.write_text(prompt, encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="usertest_backlog_") as temp_dir:
        workspace = Path(temp_dir)
        if agent == "codex":
            with _codex_host_login_env():
                run_codex_exec(
                    workspace_dir=workspace,
                    prompt=prompt,
                    raw_events_path=raw_events_path,
                    last_message_path=last_message_path,
                    stderr_path=stderr_path,
                    sandbox="read-only",
                    ask_for_approval="never",
                    binary=_agent_binary(cfg, "codex", "codex"),
                    model=model,
                    config_overrides=_codex_overrides(cfg),
                    skip_git_repo_check=True,
                )
        elif agent == "claude":
            run_claude_print(
                workspace_dir=workspace,
                prompt=prompt,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
                binary=_agent_binary(cfg, "claude", "claude"),
                output_format=_agent_output_format(cfg, "claude"),
                model=model,
                allowed_tools=[],
                permission_mode=None,
            )
        elif agent == "gemini":
            run_gemini(
                workspace_dir=workspace,
                prompt=prompt,
                raw_events_path=raw_events_path,
                last_message_path=last_message_path,
                stderr_path=stderr_path,
                binary=_agent_binary(cfg, "gemini", "gemini"),
                output_format=_agent_output_format(cfg, "gemini"),
                sandbox=False,
                model=model,
                approval_mode="default",
                allowed_tools=[],
            )
        else:
            raise ValueError(f"Unsupported backlog agent: {agent!r}")

    return _read_text(last_message_path)


def _atom_weight(atom: dict[str, Any]) -> float:
    """Compute weighted sampling score for an atom.

    Parameters
    ----------
    atom:
        Atom payload with source/severity hints.

    Returns
    -------
    float
        Sampling weight used by bagging/orphan selectors.
    """

    source = _coerce_string(atom.get("source")) or ""
    severity = _coerce_string(atom.get("severity_hint")) or "medium"

    weight = 1.0
    # Prefer canonical run_failure_event failure atoms.
    if source == "run_failure_event":
        weight *= 3.0
    elif source in {"agent_stderr"}:
        weight *= 2.0
    elif source in {"suggested_change"}:
        weight *= 1.5

    if severity in {"high", "blocker"}:
        weight *= 1.8
    elif severity == "low":
        weight *= 0.8
    return weight


def _shuffle_copy(items: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    """Return shuffled shallow copy of atom list.

    Parameters
    ----------
    items:
        Source atom list.
    rng:
        Random instance controlling determinism.

    Returns
    -------
    list[dict[str, Any]]
        Shuffled copy.
    """

    out = list(items)
    rng.shuffle(out)
    return out


def _sample_with_run_cap(
    atoms: list[dict[str, Any]],
    *,
    sample_size: int,
    rng: random.Random,
    run_cap: int,
) -> list[dict[str, Any]]:
    """Weighted sample with per-run cap to avoid single-run dominance.

    Parameters
    ----------
    atoms:
        Candidate atoms.
    sample_size:
        Requested number of atoms.
    rng:
        Random instance for deterministic sampling.
    run_cap:
        Maximum atoms per ``run_rel`` in resulting sample.

    Returns
    -------
    list[dict[str, Any]]
        Sampled atom subset honoring the run cap when possible.
    """

    if sample_size <= 0:
        return []
    if sample_size >= len(atoms):
        sampled = _shuffle_copy(atoms, rng)
    else:
        sampled = []
        pool = list(atoms)
        attempts = 0
        max_attempts = max(100, sample_size * 30)
        while len(sampled) < sample_size and pool and attempts < max_attempts:
            attempts += 1
            weights = [_atom_weight(item) for item in pool]
            picked = rng.choices(pool, weights=weights, k=1)[0]
            sampled.append(picked)
            pool.remove(picked)
        if len(sampled) < sample_size:
            remaining = [item for item in atoms if item not in sampled]
            rng.shuffle(remaining)
            sampled.extend(remaining[: sample_size - len(sampled)])

    capped: list[dict[str, Any]] = []
    run_counts: Counter[str] = Counter()
    for atom in sampled:
        run_rel = _coerce_string(atom.get("run_rel")) or "unknown"
        if run_counts[run_rel] >= run_cap:
            continue
        run_counts[run_rel] += 1
        capped.append(atom)
        if len(capped) >= sample_size:
            break

    if len(capped) < min(sample_size, len(atoms)):
        remainder = [item for item in atoms if item not in capped]
        rng.shuffle(remainder)
        for atom in remainder:
            run_rel = _coerce_string(atom.get("run_rel")) or "unknown"
            if run_counts[run_rel] >= run_cap:
                continue
            run_counts[run_rel] += 1
            capped.append(atom)
            if len(capped) >= sample_size:
                break
    return capped


def _partition_atoms(atoms: list[dict[str, Any]], parts: int) -> list[list[dict[str, Any]]]:
    """Partition atom list into round-robin chunks.

    Parameters
    ----------
    atoms:
        Ordered atom list.
    parts:
        Number of chunks to create.

    Returns
    -------
    list[list[dict[str, Any]]]
        Partitioned chunks.
    """

    if parts <= 0:
        return []
    if not atoms:
        return [[] for _ in range(parts)]

    chunks: list[list[dict[str, Any]]] = [[] for _ in range(parts)]
    for idx, atom in enumerate(atoms):
        chunks[idx % parts].append(atom)
    return chunks


def _build_miner_jobs(
    *,
    atoms: list[dict[str, Any]],
    prompt_manifest: PromptManifest,
    miners: int,
    coverage_miners: int,
    bagging_miners: int,
    sample_size: int,
    seed: int,
) -> list[MinerJob]:
    """Build deterministic coverage/bagging miner job plan.

    Parameters
    ----------
    atoms:
        Candidate atoms for mining.
    prompt_manifest:
        Prompt-template manifest.
    miners:
        Total miner count.
    coverage_miners:
        Count reserved for coverage pass.
    bagging_miners:
        Count reserved for bagging pass.
    sample_size:
        Requested atom sample size per miner.
    seed:
        Base random seed.

    Returns
    -------
    list[MinerJob]
        Ordered miner job list.
    """

    if miners <= 0:
        return []

    coverage_count = max(0, min(coverage_miners, miners))
    bagging_count = max(0, min(bagging_miners, miners - coverage_count))
    remaining = miners - coverage_count - bagging_count
    bagging_count += max(0, remaining)

    ordered = list(atoms)
    rng = random.Random(seed)
    rng.shuffle(ordered)

    jobs: list[MinerJob] = []
    coverage_chunks = _partition_atoms(ordered, coverage_count)
    coverage_templates = list(prompt_manifest.coverage_templates)
    bagging_templates = list(prompt_manifest.bagging_templates)
    sample_semantics = "all_atoms" if sample_size <= 0 else "fixed_sample"

    for idx, chunk in enumerate(coverage_chunks, start=1):
        sample_n = min(sample_size, len(chunk)) if sample_size > 0 else len(chunk)
        run_cap = 6
        sample_seed = seed + idx * 31
        capped = _sample_with_run_cap(
            chunk,
            sample_size=sample_n,
            rng=random.Random(sample_seed),
            run_cap=run_cap,
        )
        template = coverage_templates[(idx - 1) % len(coverage_templates)]
        jobs.append(
            MinerJob(
                tag=f"miner_{idx:03d}",
                template_name=template,
                atoms=capped,
                pass_type="coverage",
                selection_params={
                    "selection_strategy": "coverage_partition_weighted_sample_with_run_cap",
                    "sample_size_requested": sample_size,
                    "sample_size_effective": sample_n,
                    "sample_size_semantics": sample_semantics,
                    "run_cap": run_cap,
                    "selection_seed": sample_seed,
                    "coverage_index": idx,
                },
            )
        )

    for offset in range(bagging_count):
        idx = coverage_count + offset + 1
        bag_seed = seed + idx * 97
        bag_rng = random.Random(bag_seed)
        sample_n = min(sample_size, len(ordered)) if sample_size > 0 else len(ordered)
        run_cap = 6
        sample = _sample_with_run_cap(
            ordered,
            sample_size=sample_n,
            rng=bag_rng,
            run_cap=run_cap,
        )
        template = bagging_templates[(offset) % len(bagging_templates)]
        jobs.append(
            MinerJob(
                tag=f"miner_{idx:03d}",
                template_name=template,
                atoms=sample,
                pass_type="bagging",
                selection_params={
                    "selection_strategy": "bagging_weighted_sample_with_run_cap",
                    "sample_size_requested": sample_size,
                    "sample_size_effective": sample_n,
                    "sample_size_semantics": sample_semantics,
                    "run_cap": run_cap,
                    "selection_seed": bag_seed,
                    "bagging_index": offset + 1,
                },
            )
        )

    return jobs


def _build_miner_prompt(
    *,
    template_text: str,
    atoms: list[dict[str, Any]],
    max_tickets_per_miner: int,
) -> str:
    """Render miner prompt with atom payload substitution.

    Parameters
    ----------
    template_text:
        Prompt template text.
    atoms:
        Atom subset for this miner run.
    max_tickets_per_miner:
        Ticket cap communicated to the model.

    Returns
    -------
    str
        Fully rendered miner prompt.
    """

    atoms_payload = {
        "atoms": [
            {
                "atom_id": atom.get("atom_id"),
                "run_rel": atom.get("run_rel"),
                "agent": atom.get("agent"),
                "status": atom.get("status"),
                "source": atom.get("source"),
                "severity_hint": atom.get("severity_hint"),
                "text": atom.get("text"),
                "failure_kind": atom.get("failure_kind"),
                "error": atom.get("error"),
                "report_validation_errors": atom.get("report_validation_errors"),
                "artifacts": atom.get("artifacts"),
                "attachments": atom.get("attachments"),
                "impact": atom.get("impact"),
                "evidence": atom.get("evidence"),
                "type": atom.get("type"),
                "location": atom.get("location"),
                "priority": atom.get("priority"),
                "expected_impact": atom.get("expected_impact"),
                "report_kind": atom.get("report_kind"),
                "report_block": atom.get("report_block"),
                "report_issue_block": atom.get("report_issue_block"),
                "report_ux_block": atom.get("report_ux_block"),
                "issue_severity": atom.get("issue_severity"),
                "issue_title": atom.get("issue_title"),
                "evidence_text": atom.get("evidence_text"),
                "path_anchors": atom.get("path_anchors"),
                "linked_atom_ids": atom.get("linked_atom_ids"),
            }
            for atom in atoms
        ]
    }
    return _render_template(
        template_text,
        {
            "MAX_TICKETS_PER_MINER": str(max_tickets_per_miner),
            "ATOMS_JSON": json.dumps(atoms_payload, indent=2, ensure_ascii=False),
        },
    )


def _build_repair_prompt(raw_output: str, parse_errors: list[str], max_tickets: int) -> str:
    """Render repair prompt for malformed miner output.

    Parameters
    ----------
    raw_output:
        Original model output.
    parse_errors:
        Parsing diagnostics from first pass.
    max_tickets:
        Maximum ticket count requested in repaired output.

    Returns
    -------
    str
        Repair instruction prompt.
    """

    errors_text = "\n".join(f"- {line}" for line in parse_errors[:20])
    truncated = raw_output.strip()
    if len(truncated) > 12000:
        truncated = truncated[:12000] + "\n...[truncated]"
    return (
        "Convert the assistant output below into valid JSON only.\n"
        "Return a JSON array of ticket objects, up to "
        f"{max_tickets}.\n"
        "Each ticket must include title, severity, confidence, evidence_atom_ids, "
        "investigation_steps or proposed_fix, and success_criteria.\n"
        "Do not add explanations.\n\n"
        "Parse errors from prior attempt:\n"
        f"{errors_text}\n\n"
        "Raw output:\n"
        f"{truncated}\n"
    )


def _repair_tickets_once(
    *,
    agent: str,
    cfg: RunnerConfig,
    model: str | None,
    out_dir: Path,
    tag: str,
    raw_output: str,
    parse_errors: list[str],
    max_tickets_per_miner: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run one repair attempt for malformed ticket JSON.

    Parameters
    ----------
    agent:
        Agent identifier.
    cfg:
        Runner configuration.
    model:
        Optional model override.
    out_dir:
        Artifact directory.
    tag:
        Miner tag.
    raw_output:
        Original miner output.
    parse_errors:
        Parse diagnostics from the first parse attempt.
    max_tickets_per_miner:
        Ticket cap.

    Returns
    -------
    tuple[list[dict[str, Any]], list[str]]
        Parsed repaired tickets and parse errors from repair attempt.
    """

    repair_prompt = _build_repair_prompt(raw_output, parse_errors, max_tickets_per_miner)
    repaired_text = run_backlog_prompt(
        agent=agent,
        prompt=repair_prompt,
        out_dir=out_dir,
        tag=f"{tag}.repair",
        model=model,
        cfg=cfg,
    )
    tickets, errors = parse_ticket_list(repaired_text)
    return tickets, errors


def _load_cached_tickets(path: Path) -> list[dict[str, Any]]:
    """Load cached miner tickets from JSON artifact.

    Parameters
    ----------
    path:
        Ticket cache path.

    Returns
    -------
    list[dict[str, Any]]
        Cached ticket list when present.

    Raises
    ------
    ValueError
        Raised when cache exists but cannot be parsed as a JSON list.
    """

    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid cached tickets JSON at {path}: {exc}") from exc
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise ValueError(f"Expected cached tickets list at {path}, got {type(payload).__name__}.")


def _maybe_write_parse_error(path: Path, errors: list[str]) -> None:
    """Persist parse errors to disk when present.

    Parameters
    ----------
    path:
        Output text file path.
    errors:
        Parse error lines.
    """

    if not errors:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(errors) + "\n", encoding="utf-8")


def _run_single_miner(
    *,
    job: MinerJob,
    agent: str,
    cfg: RunnerConfig,
    model: str | None,
    prompts_dir: Path,
    prompt_manifest: PromptManifest,
    artifacts_dir: Path,
    max_tickets_per_miner: int,
    resume: bool,
    force: bool,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute one miner job including resume/dry-run behavior.

    Parameters
    ----------
    job:
        Miner job descriptor.
    agent:
        Agent identifier.
    cfg:
        Runner configuration.
    model:
        Optional model override.
    prompts_dir:
        Prompt directory.
    prompt_manifest:
        Prompt manifest.
    artifacts_dir:
        Root artifact directory.
    max_tickets_per_miner:
        Ticket cap for this run.
    resume:
        Reuse cache when input manifest matches.
    force:
        Ignore cache and rerun miner.
    dry_run:
        Write prompt without invoking agent.

    Returns
    -------
    tuple[list[dict[str, Any]], dict[str, Any]]
        Ticket list and miner metadata.
    """

    miner_dir = artifacts_dir / job.tag
    miner_dir.mkdir(parents=True, exist_ok=True)
    tickets_json_path = miner_dir / "tickets.json"
    parse_error_path = miner_dir / "parse_error.txt"
    meta_path = miner_dir / "meta.json"
    input_manifest_path = miner_dir / "input_manifest.json"
    template_sha256 = _sha256_file(prompts_dir / job.template_name)
    atoms_content_sha256 = _atoms_content_digest(job.atoms)
    cfg_sha256 = _json_digest({"agent": cfg.agents.get(agent, {}), "policies": cfg.policies})
    input_manifest = {
        "version": 2,
        "job_tag": job.tag,
        "pass_type": job.pass_type,
        "template": job.template_name,
        "template_sha256": template_sha256,
        "agent": agent,
        "model": model,
        "cfg_sha256": cfg_sha256,
        "atom_count": len(job.atoms),
        "atoms_content_sha256": atoms_content_sha256,
        "atom_ids": [
            atom_id
            for atom in job.atoms
            for atom_id in [atom.get("atom_id")]
            if isinstance(atom_id, str) and atom_id
        ],
        "selection_params": dict(job.selection_params),
        "prompt_manifest": {
            "manifest_file": _PROMPT_MANIFEST_FILENAME,
            "coverage_templates": list(prompt_manifest.coverage_templates),
            "bagging_templates": list(prompt_manifest.bagging_templates),
            "orphan_template": prompt_manifest.orphan_template,
            "merge_judge_template": prompt_manifest.merge_judge_template,
            "labeler_template": prompt_manifest.labeler_template,
        },
    }
    input_manifest_digest = _json_digest(input_manifest)
    cached_manifest = _load_json_dict(input_manifest_path)

    if (
        resume
        and not force
        and tickets_json_path.exists()
        and cached_manifest == input_manifest
    ):
        cached = _load_cached_tickets(tickets_json_path)
        allowed_atom_ids = set(input_manifest["atom_ids"])
        if _tickets_match_atom_scope(cached, allowed_atom_ids=allowed_atom_ids):
            meta = {
                "tag": job.tag,
                "pass_type": job.pass_type,
                "template": job.template_name,
                "template_sha256": template_sha256,
                "atom_count": len(job.atoms),
                "atoms_content_sha256": atoms_content_sha256,
                "input_manifest_digest": input_manifest_digest,
                "cached": True,
                "status": "ok",
                "ticket_count": len(cached),
            }
            _write_json(meta_path, meta)
            return cached, meta

    _write_json(input_manifest_path, input_manifest)

    template_text = _load_prompt_template(prompts_dir, job.template_name)
    prompt = _build_miner_prompt(
        template_text=template_text,
        atoms=job.atoms,
        max_tickets_per_miner=max_tickets_per_miner,
    )

    if dry_run:
        (miner_dir / "dry_run.prompt.txt").write_text(prompt, encoding="utf-8")
        _write_json(tickets_json_path, [])
        meta = {
            "tag": job.tag,
            "pass_type": job.pass_type,
            "template": job.template_name,
            "template_sha256": template_sha256,
            "atom_count": len(job.atoms),
            "atoms_content_sha256": atoms_content_sha256,
            "input_manifest_digest": input_manifest_digest,
            "cached": False,
            "status": "dry_run",
            "ticket_count": 0,
        }
        _write_json(meta_path, meta)
        return [], meta

    raw_text = run_backlog_prompt(
        agent=agent,
        prompt=prompt,
        out_dir=miner_dir,
        tag=job.tag,
        model=model,
        cfg=cfg,
    )
    tickets, parse_errors = parse_ticket_list(raw_text)

    if parse_errors and raw_text.strip():
        repaired_tickets, repaired_errors = _repair_tickets_once(
            agent=agent,
            cfg=cfg,
            model=model,
            out_dir=miner_dir,
            tag=job.tag,
            raw_output=raw_text,
            parse_errors=parse_errors,
            max_tickets_per_miner=max_tickets_per_miner,
        )
        if repaired_tickets:
            tickets = repaired_tickets
            parse_errors = repaired_errors

    _write_json(tickets_json_path, tickets)
    _maybe_write_parse_error(parse_error_path, parse_errors)

    status = "ok" if tickets else ("parse_failed" if parse_errors else "empty")
    meta = {
        "tag": job.tag,
        "pass_type": job.pass_type,
        "template": job.template_name,
        "template_sha256": template_sha256,
        "atom_count": len(job.atoms),
        "atoms_content_sha256": atoms_content_sha256,
        "input_manifest_digest": input_manifest_digest,
        "cached": False,
        "status": status,
        "ticket_count": len(tickets),
        "parse_errors": parse_errors,
    }
    _write_json(meta_path, meta)
    return tickets, meta


def _build_merge_judge_prompt(
    *,
    template_text: str,
    left_ticket: dict[str, Any],
    right_ticket: dict[str, Any],
    evidence_atoms: list[dict[str, Any]],
) -> str:
    """Render merge-judge prompt for one candidate pair.

    Parameters
    ----------
    template_text:
        Merge-judge template text.
    left_ticket:
        Left ticket payload.
    right_ticket:
        Right ticket payload.
    evidence_atoms:
        Supporting atoms merged from both tickets.

    Returns
    -------
    str
        Rendered merge-judge prompt text.
    """

    return _render_template(
        template_text,
        {
            "LEFT_TICKET_JSON": json.dumps(left_ticket, indent=2, ensure_ascii=False),
            "RIGHT_TICKET_JSON": json.dumps(right_ticket, indent=2, ensure_ascii=False),
            "EVIDENCE_JSON": json.dumps(evidence_atoms, indent=2, ensure_ascii=False),
        },
    )


def _parse_merge_decision(raw_text: str) -> dict[str, Any] | None:
    """Parse merge-judge decision object from raw model text.

    Parameters
    ----------
    raw_text:
        Raw agent output text.

    Returns
    -------
    dict[str, Any] | None
        Parsed decision object or ``None`` when no valid JSON object exists.
    """

    text = raw_text.strip()
    if not text:
        return None

    parsed: Any | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if not isinstance(parsed, dict):
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break

    if not isinstance(parsed, dict):
        _warn_nonfatal_fallback(
            code="merge_decision_parse_failed",
            message="Merge-judge output was not valid JSON; leaving pair unmerged.",
        )
        return None
    same_issue = bool(parsed.get("same_issue"))
    decision: dict[str, Any] = {"same_issue": same_issue}
    merged = parsed.get("merged_ticket")
    if isinstance(merged, dict):
        decision["merged_ticket"] = merged
    return decision


def _fallback_merge_ticket(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Synthesize merged ticket via deterministic domain dedupe fallback.

    Parameters
    ----------
    left:
        Left ticket candidate.
    right:
        Right ticket candidate.

    Returns
    -------
    dict[str, Any]
        Best-effort merged ticket payload.
    """

    merged_list = dedupe_tickets([left, right])
    if not merged_list:
        _warn_nonfatal_fallback(
            code="merge_fallback_empty",
            message=(
                "Fallback dedupe produced no merged ticket; reusing left candidate as "
                "best effort."
            ),
        )
    return merged_list[0] if merged_list else dict(left)


def _run_merge_judge(
    *,
    agent: str,
    cfg: RunnerConfig,
    model: str | None,
    prompts_dir: Path,
    prompt_manifest: PromptManifest,
    artifacts_dir: Path,
    tickets: list[dict[str, Any]],
    atoms_by_id: dict[str, dict[str, Any]],
    resume: bool,
    force: bool,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Run merge-judge stage and collapse tickets when same-issue is confirmed.

    Parameters
    ----------
    agent:
        Agent identifier.
    cfg:
        Runner configuration.
    model:
        Optional model override.
    prompts_dir:
        Prompt directory.
    prompt_manifest:
        Prompt manifest.
    artifacts_dir:
        Root artifact directory.
    tickets:
        Current deduped ticket list.
    atoms_by_id:
        Atom lookup map.
    resume:
        Reuse cached merge decisions.
    force:
        Ignore merge-decision cache.
    dry_run:
        Write placeholder non-merge decisions.

    Returns
    -------
    tuple[list[dict[str, Any]], int]
        Merged ticket list and number of merge decisions evaluated.
    """

    candidates = build_merge_candidates(tickets)
    if not candidates:
        return tickets, 0

    template = _load_prompt_template(prompts_dir, prompt_manifest.merge_judge_template)

    template_sha256 = _sha256_text(template)
    cfg_sha256 = _json_digest({"agent": cfg.agents.get(agent, {}), "policies": cfg.policies})
    max_evidence_atoms = _env_int("BACKLOG_MERGE_JUDGE_MAX_EVIDENCE_ATOMS", 25)
    max_evidence_chars = _env_int("BACKLOG_MERGE_JUDGE_MAX_EVIDENCE_CHARS", 60000)

    merged_working = list(tickets)
    inactive: set[int] = set()
    decisions = 0

    for index, (left_idx, right_idx) in enumerate(candidates, start=1):
        if left_idx in inactive or right_idx in inactive:
            continue
        if left_idx >= len(merged_working) or right_idx >= len(merged_working):
            continue

        left = merged_working[left_idx]
        right = merged_working[right_idx]

        left_ids = [item for item in left.get("evidence_atom_ids", []) if isinstance(item, str)]
        right_ids = [item for item in right.get("evidence_atom_ids", []) if isinstance(item, str)]
        evidence_ids = sorted(set([*left_ids, *right_ids]))
        evidence_atoms, evidence_ids_included = _select_evidence_atoms(
            atoms_by_id,
            evidence_ids,
            max_atoms=max_evidence_atoms,
            max_total_chars=max_evidence_chars,
        )

        judge_dir = artifacts_dir / "merge_judge"
        judge_dir.mkdir(parents=True, exist_ok=True)
        tag = f"pair_{index:03d}"
        decision_path = judge_dir / f"{tag}.decision.json"
        input_path = judge_dir / f"{tag}.input.json"
        decision_input = {
            "version": 1,
            "tag": tag,
            "template": prompt_manifest.merge_judge_template,
            "template_sha256": template_sha256,
            "agent": agent,
            "model": model,
            "cfg_sha256": cfg_sha256,
            "max_evidence_atoms": max_evidence_atoms,
            "max_evidence_chars": max_evidence_chars,
            "evidence_atom_ids_total": evidence_ids,
            "evidence_atom_ids_included": evidence_ids_included,
            "evidence_atoms_sha256": _atoms_content_digest(evidence_atoms),
            "left_anchor": _ticket_anchor(left),
            "right_anchor": _ticket_anchor(right),
        }

        if (
            resume
            and not force
            and decision_path.exists()
            and _load_json_dict(input_path) == decision_input
        ):
            parsed = _load_json_dict(decision_path)
            decision_payload = parsed if parsed is not None else {"same_issue": False}
        elif dry_run:
            decision_payload = {"same_issue": False, "dry_run": True}
            _write_json(input_path, decision_input)
            _write_json(decision_path, decision_payload)
        else:
            prompt = _build_merge_judge_prompt(
                template_text=template,
                left_ticket=left,
                right_ticket=right,
                evidence_atoms=evidence_atoms,
            )
            raw_text = run_backlog_prompt(
                agent=agent,
                prompt=prompt,
                out_dir=judge_dir,
                tag=tag,
                model=model,
                cfg=cfg,
            )
            parsed = _parse_merge_decision(raw_text)
            if parsed is None:
                parsed = {"same_issue": False, "parse_failed": True}
            decision_payload = parsed
            _write_json(input_path, decision_input)
            _write_json(decision_path, decision_payload)

        decisions += 1
        if not bool(decision_payload.get("same_issue")):
            continue

        merged_ticket_raw = decision_payload.get("merged_ticket")
        merged_ticket: dict[str, Any] | None = None
        if isinstance(merged_ticket_raw, dict):
            merged_json = json.dumps([merged_ticket_raw], ensure_ascii=False)
            parsed_tickets, _ = parse_ticket_list(merged_json)
            if parsed_tickets:
                merged_ticket = parsed_tickets[0]

        if merged_ticket is None:
            merged_ticket = _fallback_merge_ticket(left, right)

        inactive.add(left_idx)
        inactive.add(right_idx)
        merged_working.append(merged_ticket)

    final = [ticket for idx, ticket in enumerate(merged_working) if idx not in inactive]
    return dedupe_tickets(final), decisions


def _run_orphan_passes(
    *,
    agent: str,
    cfg: RunnerConfig,
    model: str | None,
    prompts_dir: Path,
    prompt_manifest: PromptManifest,
    artifacts_dir: Path,
    atoms: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    sample_size: int,
    max_tickets_per_miner: int,
    orphan_passes: int,
    seed: int,
    resume: bool,
    force: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Run orphan high-severity passes to recover uncovered issues.

    Parameters
    ----------
    agent:
        Agent identifier.
    cfg:
        Runner configuration.
    model:
        Optional model override.
    prompts_dir:
        Prompt directory.
    prompt_manifest:
        Prompt manifest.
    artifacts_dir:
        Root artifact directory.
    atoms:
        Full atom list.
    tickets:
        Current ticket list.
    sample_size:
        Sampling size per orphan pass.
    max_tickets_per_miner:
        Ticket cap for each orphan run.
    orphan_passes:
        Number of orphan passes to attempt.
    seed:
        Base random seed.
    resume:
        Reuse orphan miner cache.
    force:
        Ignore orphan miner cache.
    dry_run:
        Write prompts only.

    Returns
    -------
    list[dict[str, Any]]
        Ticket list after orphan recovery passes.
    """

    current = list(tickets)
    for pass_idx in range(1, max(0, orphan_passes) + 1):
        coverage = compute_backlog_coverage(atoms, current)
        uncovered_ids_raw = coverage.get("uncovered_high_severity_atom_ids")
        uncovered_ids = (
            [item for item in uncovered_ids_raw if isinstance(item, str) and item.strip()]
            if isinstance(uncovered_ids_raw, list)
            else []
        )
        if not uncovered_ids:
            break

        atom_ids = set(uncovered_ids)
        orphan_atoms = [
            atom
            for atom in atoms
            if isinstance(atom.get("atom_id"), str) and atom["atom_id"] in atom_ids
        ]
        if not orphan_atoms:
            break

        selection_seed = seed + pass_idx * 911
        rng = random.Random(selection_seed)
        sample_n = (
            min(sample_size, len(orphan_atoms))
            if sample_size > 0
            else len(orphan_atoms)
        )
        run_cap = 8
        subset = _sample_with_run_cap(
            orphan_atoms,
            sample_size=sample_n,
            rng=rng,
            run_cap=run_cap,
        )

        job = MinerJob(
            tag=f"orphan_{pass_idx:03d}",
            template_name=prompt_manifest.orphan_template,
            atoms=subset,
            pass_type="orphan",
            selection_params={
                "selection_strategy": "orphan_high_severity_weighted_sample_with_run_cap",
                "sample_size_requested": sample_size,
                "sample_size_effective": sample_n,
                "sample_size_semantics": "all_atoms" if sample_size <= 0 else "fixed_sample",
                "run_cap": run_cap,
                "selection_seed": selection_seed,
                "orphan_pass_index": pass_idx,
                "uncovered_high_severity_atoms": len(orphan_atoms),
            },
        )

        orphan_dir = artifacts_dir / "orphan_pass"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        new_tickets, _ = _run_single_miner(
            job=job,
            agent=agent,
            cfg=cfg,
            model=model,
            prompts_dir=prompts_dir,
            prompt_manifest=prompt_manifest,
            artifacts_dir=orphan_dir,
            max_tickets_per_miner=max_tickets_per_miner,
            resume=resume,
            force=force,
            dry_run=dry_run,
        )
        if not new_tickets:
            continue
        current = dedupe_tickets([*current, *new_tickets])
    return current


def run_backlog_ensemble(
    *,
    atoms: list[dict[str, Any]],
    artifacts_dir: Path,
    prompts_dir: Path,
    prompt_manifest: PromptManifest,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    miners: int,
    sample_size: int,
    coverage_miners: int,
    bagging_miners: int,
    max_tickets_per_miner: int,
    seed: int,
    resume: bool,
    force: bool,
    dry_run: bool,
    no_merge: bool,
    orphan_pass: int,
) -> dict[str, Any]:
    """Execute full backlog mining pipeline and return tickets plus run metadata.

    Parameters
    ----------
    atoms:
        Input backlog atoms.
    artifacts_dir:
        Root directory for mining artifacts.
    prompts_dir:
        Prompt template directory.
    prompt_manifest:
        Parsed prompt manifest.
    agent:
        Agent identifier.
    model:
        Optional model override.
    cfg:
        Runner configuration.
    miners:
        Total number of miner jobs.
    sample_size:
        Atom sample size per miner (`<=0` means all atoms).
    coverage_miners:
        Number of coverage miners.
    bagging_miners:
        Number of bagging miners.
    max_tickets_per_miner:
        Ticket cap per miner call.
    seed:
        Base random seed.
    resume:
        Reuse cached miner/merge artifacts.
    force:
        Ignore cached miner/merge artifacts.
    dry_run:
        Write prompts and metadata without invoking agents.
    no_merge:
        Skip merge-judge stage.
    orphan_pass:
        Number of orphan recovery passes.

    Returns
    -------
    dict[str, Any]
        Ticket payload and miners metadata.
    """

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    jobs = _build_miner_jobs(
        atoms=atoms,
        prompt_manifest=prompt_manifest,
        miners=miners,
        coverage_miners=coverage_miners,
        bagging_miners=bagging_miners,
        sample_size=sample_size,
        seed=seed,
    )

    mined_tickets: list[dict[str, Any]] = []
    job_meta: list[dict[str, Any]] = []

    for job in jobs:
        tickets, meta = _run_single_miner(
            job=job,
            agent=agent,
            cfg=cfg,
            model=model,
            prompts_dir=prompts_dir,
            prompt_manifest=prompt_manifest,
            artifacts_dir=artifacts_dir,
            max_tickets_per_miner=max_tickets_per_miner,
            resume=resume,
            force=force,
            dry_run=dry_run,
        )
        mined_tickets.extend(tickets)
        job_meta.append(meta)

    tickets = dedupe_tickets(mined_tickets)

    atoms_by_id: dict[str, dict[str, Any]] = {}
    for atom in atoms:
        atom_id = atom.get("atom_id")
        if isinstance(atom_id, str):
            atoms_by_id[atom_id] = atom
    merge_decisions = 0
    if not no_merge:
        tickets, merge_decisions = _run_merge_judge(
            agent=agent,
            cfg=cfg,
            model=model,
            prompts_dir=prompts_dir,
            prompt_manifest=prompt_manifest,
            artifacts_dir=artifacts_dir,
            tickets=tickets,
            atoms_by_id=atoms_by_id,
            resume=resume,
            force=force,
            dry_run=dry_run,
        )

    tickets = _run_orphan_passes(
        agent=agent,
        cfg=cfg,
        model=model,
        prompts_dir=prompts_dir,
        prompt_manifest=prompt_manifest,
        artifacts_dir=artifacts_dir,
        atoms=atoms,
        tickets=tickets,
        sample_size=sample_size,
        max_tickets_per_miner=max_tickets_per_miner,
        orphan_passes=orphan_pass,
        seed=seed,
        resume=resume,
        force=force,
        dry_run=dry_run,
    )

    anchors_seen: set[str] = set()
    unique_tickets: list[dict[str, Any]] = []
    for ticket in tickets:
        anchor = _ticket_anchor(ticket)
        if anchor in anchors_seen:
            continue
        anchors_seen.add(anchor)
        unique_tickets.append(ticket)

    miners_completed = sum(
        1 for item in job_meta if item.get("status") in {"ok", "dry_run", "empty"}
    )
    miners_failed = sum(1 for item in job_meta if item.get("status") == "parse_failed")

    return {
        "tickets": unique_tickets,
        "miners_meta": {
            "miners_total": len(jobs),
            "miners_completed": miners_completed,
            "miners_failed": miners_failed,
            "merge_decisions": merge_decisions,
            "sample_size_semantics": "all_atoms" if sample_size <= 0 else "fixed_sample",
            "sample_size_requested": sample_size,
            "orphan_passes_requested": orphan_pass,
            "prompt_manifest": {
                "manifest_file": _PROMPT_MANIFEST_FILENAME,
                "coverage_templates": list(prompt_manifest.coverage_templates),
                "bagging_templates": list(prompt_manifest.bagging_templates),
                "orphan_template": prompt_manifest.orphan_template,
                "merge_judge_template": prompt_manifest.merge_judge_template,
                "labeler_template": prompt_manifest.labeler_template,
            },
            "jobs": job_meta,
        },
    }
