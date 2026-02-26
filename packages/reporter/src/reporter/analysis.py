from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_artifacts.capture import CaptureResult, TextCapturePolicy, capture_text_artifact
from run_artifacts.run_failure_event import (
    classify_failure_kind,
    coerce_validation_errors,
    extract_error_artifacts,
    render_failure_text,
    sanitize_error,
)


@dataclass(frozen=True)
class _ThemeRule:
    theme_id: str
    title: str
    patterns: tuple[re.Pattern[str], ...]


def _make_rule(theme_id: str, title: str, *patterns: str) -> _ThemeRule:
    compiled = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    return _ThemeRule(theme_id=theme_id, title=title, patterns=compiled)


_THEME_RULES: tuple[_ThemeRule, ...] = (
    _make_rule(
        "execution_permissions",
        "Execution Permissions and Harness Limits",
        r"agentexecfailed",
        r"permission_policy",
        r"trusted command list",
        r"commands? (are )?blocked",
        r"interactive approval",
        r"ask_for_approval",
        r"apply_patch_approval_request",
        r"unable to execute .*tooling",
        r"tool execution denied by policy",
        r"tool .*not found in registry",
        r"did you mean one of:",
        r"errorredactor",
    ),
    _make_rule(
        "target_context_contract",
        "Target Context Contract",
        r"no `?users\.md`?",
        r"missing users\.md",
        r"without users\.md",
        r"use_builtin_context",
        r"mission prompt references it",
    ),
    _make_rule(
        "output_contract",
        "Output Contract Compliance",
        r"failed to parse json",
        r"could not find a json object",
        r"return only.*json",
        r"produced.*json output",
        r"\$?\.?outputs\[\d+\]\.path: none is not of type 'string'",
    ),
    _make_rule(
        "output_envelope",
        "Structured Output Envelope (Informational)",
        r"agent_last_message report_json_envelope",
        r"agent_last_message json_object_keys",
        r"agent_last_message json_array_len",
    ),
    _make_rule(
        "docs_discoverability",
        "Discoverability and Quickstart",
        r"quick\s*start",
        r"no documentation",
        r"no usage examples",
        r"readme",
        r"examples?",
    ),
    _make_rule(
        "version_metadata",
        "Version and Metadata Introspection",
        r"__version__",
        r"programmatic version",
        r"top_level\.txt",
    ),
    _make_rule(
        "entrypoint_ux",
        "Entrypoint and CLI UX",
        r"entry points? (are )?installed",
        r"console script",
        r"canonical cli",
        r"python -m agent_adapters",
    ),
    _make_rule(
        "environment_determinism",
        "Environment Determinism",
        r"precreated venv",
        r"virtualenv",
        r"\bvenv\b",
        r"default `python`/`pip`",
        r"\bPATH\b",
    ),
    _make_rule(
        "sandbox_scope",
        "Sandbox Scope and Pathing",
        r"path not in workspace",
        r"outside the allowed workspace",
        r"sandbox",
    ),
    _make_rule(
        "provider_capacity",
        "Provider Capacity and Quotas",
        r"provider_capacity",
        r"no capacity available",
        r"resource_exhausted",
        r"model_capacity_exhausted",
        r"hit your limit",
        r"resets \d",
        r"\b429\b",
        r"quota",
    ),
    _make_rule(
        "adapter_normalization",
        "Adapter Normalization Correctness",
        r"normalize",
        r"did not emit `read_file`",
        r"read attribution",
        r"read_path inference",
        r"cd <dir> && <readlike>",
    ),
    _make_rule(
        "binary_preflight",
        "Binary Preflight and Launch Diagnostics",
        r"agentpreflightfailed",
        r"binary_missing",
        r"binary_or_command_missing",
        r"required agent binary",
        r"binary not found",
        r"check_binary",
        r"file not found",
        r"could not launch",
    ),
    _make_rule(
        "sandbox_tooling",
        "Sandbox Tooling Baseline",
        r"pgrep: .*not found",
    ),
    _make_rule(
        "runtime_process",
        "Runtime and Process Failures",
        r"\bruntimeerror\b",
        r"\btraceback\b",
        r"error: node",
        r"node:\d+:\d+",
        r"process exited",
        r"exit code \d+",
    ),
)

_SIMILARITY_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_SIMILARITY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "no",
    "not",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}
_MARKDOWN_SIGNAL_PREVIEW_MAX_CHARS = 420
_DEFAULT_CAPTURE_POLICY = TextCapturePolicy(
    max_excerpt_bytes=24_000,
    head_bytes=12_000,
    tail_bytes=12_000,
    max_line_count=300,
    binary_detection_bytes=2_048,
)


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except Exception:  # noqa: BLE001
        return str(path).replace("\\", "/")


def _compose_artifact_signal_text(result: CaptureResult) -> str:
    excerpt = result.excerpt
    error = result.error
    if excerpt is None:
        return f"[capture_error] {error}" if isinstance(error, str) and error else ""
    if not excerpt.truncated:
        return excerpt.head
    marker = "\n...[truncated; see capture_manifest]...\n"
    if excerpt.head and excerpt.tail:
        return excerpt.head + marker + excerpt.tail
    return excerpt.head or excerpt.tail


def _capture_meta(result: CaptureResult) -> dict[str, Any]:
    artifact = result.artifact
    meta: dict[str, Any] = {
        "artifact_ref": {
            "path": artifact.path,
            "exists": artifact.exists,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        },
        "capture_error": result.error,
        "truncated": bool(result.excerpt.truncated) if result.excerpt is not None else False,
    }
    if result.excerpt is not None:
        meta["excerpt_head"] = result.excerpt.head
        meta["excerpt_tail"] = result.excerpt.tail
    return meta


def _capture_manifest_entry(result: CaptureResult) -> dict[str, Any]:
    artifact = result.artifact
    entry: dict[str, Any] = {
        "path": artifact.path,
        "abs_path": artifact.abs_path,
        "exists": artifact.exists,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "error": result.error,
        "truncated": bool(result.excerpt.truncated) if result.excerpt is not None else False,
    }
    if result.excerpt is not None:
        entry["excerpt_head_chars"] = len(result.excerpt.head)
        entry["excerpt_tail_chars"] = len(result.excerpt.tail)
    return entry


def _classify_theme(text: str) -> tuple[str, str]:
    for rule in _THEME_RULES:
        if any(p.search(text) for p in rule.patterns):
            return rule.theme_id, rule.title
    return "other", "Other / Unclassified"


def _classify_themes(text: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for rule in _THEME_RULES:
        if any(p.search(text) for p in rule.patterns):
            matches.append((rule.theme_id, rule.title))
    if matches:
        return matches
    return [("other", "Other / Unclassified")]


def _normalize_similarity_key(text: str) -> str:
    tokens = [
        token
        for token in _SIMILARITY_TOKEN_RE.findall(text.lower())
        if token not in _SIMILARITY_STOPWORDS
    ]
    if not tokens:
        return "other"
    return " ".join(tokens[:14])


def _to_singleline_display(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").strip()


def _try_parse_json_blob(text: str) -> Any | None:
    candidate = text.strip()
    if not candidate:
        return None
    if not (
        (candidate.startswith("{") and candidate.endswith("}"))
        or (candidate.startswith("[") and candidate.endswith("]"))
    ):
        return None
    try:
        return json.loads(candidate)
    except Exception:  # noqa: BLE001
        return None


def _try_parse_json_anywhere(text: str) -> Any | None:
    cleaned = text.strip()
    if not cleaned:
        return None

    decoder = json.JSONDecoder()
    for idx, char in enumerate(cleaned):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[idx:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _normalize_signal_text(*, source: str, text: str) -> tuple[str, str | None]:
    cleaned = text.strip()
    if not cleaned:
        return "", None

    parsed = _try_parse_json_blob(cleaned)
    if parsed is None and source == "agent_last_message":
        parsed = _try_parse_json_anywhere(cleaned)
    if source == "agent_last_message" and isinstance(parsed, dict):
        if {"schema_version", "persona", "mission"}.issubset(set(parsed)):
            recommendation = None
            adoption = parsed.get("adoption_decision")
            if isinstance(adoption, dict):
                recommendation_raw = adoption.get("recommendation")
                if isinstance(recommendation_raw, str) and recommendation_raw.strip():
                    recommendation = recommendation_raw.strip()
            confusion_points = parsed.get("confusion_points")
            confusion_count = (
                len(confusion_points) if isinstance(confusion_points, list) else 0
            )
            suggested_changes = parsed.get("suggested_changes")
            suggested_count = (
                len(suggested_changes) if isinstance(suggested_changes, list) else 0
            )
            missing_count = 0
            confidence = parsed.get("confidence_signals")
            if isinstance(confidence, dict) and isinstance(confidence.get("missing"), list):
                missing_count = len(confidence["missing"])
            parts = [
                "agent_last_message report_json_envelope",
                f"confusion_points {confusion_count}",
                f"suggested_changes {suggested_count}",
                f"confidence_missing {missing_count}",
            ]
            if recommendation is not None:
                parts.append(f"recommendation {recommendation}")
            return "; ".join(parts), "report_json_envelope"

        keys = [str(k) for k in parsed.keys()][:10]
        return f"agent_last_message json_object_keys {' '.join(keys)}", "json_object_envelope"

    if source == "agent_last_message" and isinstance(parsed, list):
        return f"agent_last_message json_array_len {len(parsed)}", "json_array_envelope"

    return cleaned, None


def _format_signal_preview(signal: dict[str, Any]) -> str:
    raw_text = signal.get("raw_text")
    raw_text_s = raw_text if isinstance(raw_text, str) else ""
    display_raw = _to_singleline_display(raw_text_s)
    normalized_kind = signal.get("normalization_kind")
    normalized_text = signal.get("normalized_text")
    normalized_text_s = (
        normalized_text if isinstance(normalized_text, str) else display_raw
    )

    if isinstance(normalized_kind, str) and normalized_kind:
        preview = f"[normalized:{normalized_kind}] {normalized_text_s}"
    else:
        preview = display_raw

    if len(preview) > _MARKDOWN_SIGNAL_PREVIEW_MAX_CHARS:
        preview = (
            preview[: _MARKDOWN_SIGNAL_PREVIEW_MAX_CHARS]
            + "… [truncated; see `raw_text` in JSON]"
        )

    signal_id_raw = signal.get("signal_id")
    if isinstance(signal_id_raw, str) and signal_id_raw.strip():
        preview += f" [signal_id: `{signal_id_raw.strip()}`]"
    return preview


def _iter_report_signals(report: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    confusion_raw = report.get("confusion_points")
    if isinstance(confusion_raw, list):
        for item in confusion_raw:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary")
            if isinstance(summary, str) and summary.strip():
                out.append(("confusion_point", summary.strip()))

    suggested_raw = report.get("suggested_changes")
    if isinstance(suggested_raw, list):
        for item in suggested_raw:
            if not isinstance(item, dict):
                continue
            change = item.get("change")
            if isinstance(change, str) and change.strip():
                out.append(("suggested_change", change.strip()))

    confidence_raw = report.get("confidence_signals")
    if isinstance(confidence_raw, dict):
        missing_raw = confidence_raw.get("missing")
        if isinstance(missing_raw, list):
            for value in missing_raw:
                if isinstance(value, str) and value.strip():
                    out.append(("confidence_missing", value.strip()))

    return out


def _coerce_string_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
    return tuple(values)


def _load_issue_actions(path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
        "actions_file": str(path) if isinstance(path, Path) else None,
        "loaded_actions": 0,
        "skipped_actions": 0,
    }
    if path is None:
        return [], meta
    if not path.exists():
        raise FileNotFoundError(f"Issue actions file not found: {path}")

    raw_data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raise ValueError("Issue actions file must be a JSON object.")

    raw_actions = raw_data.get("actions")
    if not isinstance(raw_actions, list):
        raise ValueError("Issue actions file must contain `actions: []`.")

    loaded: list[dict[str, Any]] = []
    skipped = 0
    for item in raw_actions:
        if not isinstance(item, dict):
            skipped += 1
            continue

        action_id_raw = item.get("id")
        action_date_raw = item.get("date")
        action_plan_raw = item.get("plan")
        if (
            not isinstance(action_id_raw, str)
            or not action_id_raw.strip()
            or not isinstance(action_date_raw, str)
            or not action_date_raw.strip()
            or not isinstance(action_plan_raw, str)
            or not action_plan_raw.strip()
        ):
            skipped += 1
            continue

        match_raw = item.get("match")
        if not isinstance(match_raw, dict):
            skipped += 1
            continue

        theme_ids = _coerce_string_list(match_raw.get("theme_ids"))
        sources = _coerce_string_list(match_raw.get("sources"))
        signatures = _coerce_string_list(match_raw.get("signatures"))
        contains_any = tuple(
            value.lower()
            for value in _coerce_string_list(match_raw.get("contains_any"))
        )
        raw_patterns = _coerce_string_list(match_raw.get("text_patterns"))
        text_patterns: list[re.Pattern[str]] = []
        bad_pattern = False
        for pattern in raw_patterns:
            try:
                text_patterns.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                bad_pattern = True
                break
        if bad_pattern:
            skipped += 1
            continue
        if (
            not theme_ids
            and not sources
            and not signatures
            and not contains_any
            and not text_patterns
        ):
            skipped += 1
            continue

        note_raw = item.get("note")
        note = (
            note_raw.strip() if isinstance(note_raw, str) and note_raw.strip() else None
        )
        loaded.append(
            {
                "id": action_id_raw.strip(),
                "date": action_date_raw.strip(),
                "plan": action_plan_raw.strip(),
                "note": note,
                "theme_ids": theme_ids,
                "sources": sources,
                "signatures": signatures,
                "contains_any": contains_any,
                "text_patterns": tuple(text_patterns),
            }
        )

    meta["loaded_actions"] = len(loaded)
    meta["skipped_actions"] = skipped
    return loaded, meta


def _action_matches_signal(
    action: dict[str, Any],
    *,
    theme_id: str,
    source: str,
    signature: str,
    raw_signature: str,
    text: str,
) -> bool:
    theme_ids = action.get("theme_ids")
    if isinstance(theme_ids, tuple) and theme_ids and theme_id not in theme_ids:
        return False

    sources = action.get("sources")
    if isinstance(sources, tuple) and sources and source not in sources:
        return False

    signatures = action.get("signatures")
    if (
        isinstance(signatures, tuple)
        and signatures
        and signature not in signatures
        and raw_signature not in signatures
    ):
        return False

    text_lc = text.lower()
    contains_any = action.get("contains_any")
    if isinstance(contains_any, tuple) and contains_any:
        if not any(isinstance(token, str) and token and token in text_lc for token in contains_any):
            return False

    text_patterns = action.get("text_patterns")
    if isinstance(text_patterns, tuple) and text_patterns:
        if not any(
            isinstance(pattern, re.Pattern) and pattern.search(text)
            for pattern in text_patterns
        ):
            return False

    return True


def _match_issue_action(
    actions: list[dict[str, Any]],
    *,
    theme_id: str,
    source: str,
    signature: str,
    raw_signature: str,
    text: str,
) -> dict[str, Any] | None:
    for action in actions:
        if _action_matches_signal(
            action,
            theme_id=theme_id,
            source=source,
            signature=signature,
            raw_signature=raw_signature,
            text=text,
        ):
            return action
    return None


def analyze_report_history(
    records: list[dict[str, Any]],
    *,
    repo_root: Path | None = None,
    issue_actions_path: Path | None = None,
    capture_policy: TextCapturePolicy | None = None,
) -> dict[str, Any]:
    policy = capture_policy or _DEFAULT_CAPTURE_POLICY
    status_counts: Counter[str] = Counter()
    recommendation_counts: Counter[str] = Counter()
    agent_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()

    theme_title_by_id: dict[str, str] = {}
    theme_run_ids: dict[str, set[str]] = defaultdict(set)
    theme_agents: dict[str, set[str]] = defaultdict(set)
    theme_sources: dict[str, set[str]] = defaultdict(set)
    theme_examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    theme_similarity: dict[str, Counter[str]] = defaultdict(Counter)
    theme_similarity_addressed: dict[str, Counter[str]] = defaultdict(Counter)
    theme_similarity_unaddressed: dict[str, Counter[str]] = defaultdict(Counter)
    theme_signals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    normalization_counts: Counter[str] = Counter()

    actions, actions_meta = _load_issue_actions(issue_actions_path)
    total_addressed_mentions = 0

    run_summaries: list[dict[str, Any]] = []
    capture_manifest: dict[str, list[dict[str, Any]]] = {}

    for record in records:
        run_dir_raw = record.get("run_dir")
        run_dir = str(run_dir_raw) if isinstance(run_dir_raw, str) else "<unknown>"
        run_id = str(record.get("run_rel") or run_dir)
        run_path_display = run_dir
        if repo_root is not None:
            run_path_display = _safe_relpath(Path(run_dir), repo_root)
        agent = str(record.get("agent") or "unknown")
        status = str(record.get("status") or "unknown")

        status_counts[status] += 1
        agent_counts[agent] += 1

        report = record.get("report")
        recommendation: str | None = None
        if isinstance(report, dict):
            adoption = report.get("adoption_decision")
            if isinstance(adoption, dict):
                rec = adoption.get("recommendation")
                if isinstance(rec, str) and rec.strip():
                    recommendation = rec.strip()
                    recommendation_counts[recommendation] += 1

        signals: list[tuple[str, str, dict[str, Any]]] = []
        if isinstance(report, dict):
            signals.extend((source, text, {}) for source, text in _iter_report_signals(report))

        validation_values = coerce_validation_errors(record.get("report_validation_errors"))
        sanitized_error = sanitize_error(record.get("error"))
        artifacts = extract_error_artifacts(sanitized_error)
        is_failure, failure_kind = classify_failure_kind(
            status=status,
            error=sanitized_error,
            validation_errors=validation_values,
        )

        run_dir_path = Path(run_dir)
        run_capture_entries: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []
        for filename, source in (
            ("agent_stderr.txt", "agent_stderr"),
            ("agent_last_message.txt", "agent_last_message"),
        ):
            capture = capture_text_artifact(
                run_dir_path / filename, policy=policy, root=run_dir_path
            )
            run_capture_entries.append(_capture_manifest_entry(capture))
            if is_failure:
                attachment: dict[str, Any] = {"path": capture.artifact.path}
                attachment.update(_capture_meta(capture))
                attachments.append(attachment)
                continue
            if not capture.artifact.exists:
                continue
            if (
                source == "agent_stderr"
                and status.strip().lower() == "ok"
                and capture.artifact.size_bytes == 0
            ):
                continue
            capture_text = _compose_artifact_signal_text(capture).strip()
            if not capture_text:
                capture_text = "[empty artifact]"
            signal_meta = _capture_meta(capture)
            signals.append((source, capture_text, signal_meta))
        capture_manifest[run_id] = run_capture_entries

        if is_failure:
            failure_text = render_failure_text(
                failure_kind=failure_kind,
                agent=agent,
                status=status,
                error=sanitized_error,
                report_validation_errors=validation_values,
                artifacts=artifacts,
                attachments=attachments,
            )
            signals.append(
                (
                    "run_failure_event",
                    failure_text,
                    {
                        "failure_kind": failure_kind,
                        "error": sanitized_error,
                        "report_validation_errors": validation_values,
                        "artifacts": artifacts,
                        "attachments": attachments,
                    },
                )
            )

        run_signal_index = 0
        for source, text, signal_meta in signals:
            run_signal_index += 1
            raw_text = text
            normalized_text, normalization_kind = _normalize_signal_text(
                source=source, text=raw_text
            )
            classification_text = normalized_text if normalized_text else raw_text
            theme_matches = (
                _classify_themes(classification_text)
                if source == "run_failure_event"
                else [_classify_theme(classification_text)]
            )
            signature = _normalize_similarity_key(classification_text)
            raw_signature = _normalize_similarity_key(raw_text)
            display_text = _to_singleline_display(raw_text)
            normalized_display_text = _to_singleline_display(
                normalized_text if normalized_text else raw_text
            )
            if isinstance(normalization_kind, str) and normalization_kind:
                normalization_counts[normalization_kind] += 1

            for theme_id, title in theme_matches:
                action = _match_issue_action(
                    actions,
                    theme_id=theme_id,
                    source=source,
                    signature=signature,
                    raw_signature=raw_signature,
                    text=raw_text,
                )
                addressed = action is not None

                theme_title_by_id[theme_id] = title
                theme_run_ids[theme_id].add(run_id)
                theme_agents[theme_id].add(agent)
                theme_sources[theme_id].add(source)
                theme_similarity[theme_id][signature] += 1
                if addressed:
                    theme_similarity_addressed[theme_id][signature] += 1
                    total_addressed_mentions += 1
                else:
                    theme_similarity_unaddressed[theme_id][signature] += 1
                issue_counts[theme_id] += 1

                signal_item: dict[str, Any] = {
                    "run_dir": run_path_display,
                    "run_id": run_id,
                    "signal_id": f"{run_id}:{run_signal_index}",
                    "agent": agent,
                    "source": source,
                    "signature": signature,
                    "text": display_text,
                    "raw_text": raw_text,
                    "normalized_text": normalized_display_text,
                    "normalization_kind": normalization_kind,
                    "addressed": addressed,
                }
                if isinstance(signal_meta, dict):
                    for key, value in signal_meta.items():
                        if key in signal_item or value is None:
                            continue
                        signal_item[key] = value
                if addressed and isinstance(action, dict):
                    signal_item["action_id"] = action.get("id")
                    signal_item["action_date"] = action.get("date")
                    signal_item["action_plan"] = action.get("plan")
                    signal_item["action_note"] = action.get("note")
                theme_signals[theme_id].append(signal_item)

                examples = theme_examples[theme_id]
                if len(examples) < 4:
                    example_text = (
                        f"[normalized:{normalization_kind}] {normalized_display_text}"
                        if isinstance(normalization_kind, str) and normalization_kind
                        else display_text
                    )
                    examples.append(
                        {
                            "run_dir": run_path_display,
                            "agent": agent,
                            "source": source,
                            "text": example_text[:280],
                        }
                    )

        run_summaries.append(
            {
                "run_dir": run_dir,
                "agent": agent,
                "status": status,
                "recommendation": recommendation,
                "issue_signals": len(signals),
            }
        )

    themes: list[dict[str, Any]] = []
    for theme_id, mentions in issue_counts.most_common():
        similarity_counter = theme_similarity.get(theme_id, Counter())
        addressed_counter = theme_similarity_addressed.get(theme_id, Counter())
        unaddressed_counter = theme_similarity_unaddressed.get(theme_id, Counter())
        similarity_clusters = [
            {
                "signature": signature,
                "mentions": count,
                "unaddressed_mentions": int(unaddressed_counter.get(signature, 0)),
                "addressed_mentions": int(addressed_counter.get(signature, 0)),
            }
            for signature, count in similarity_counter.most_common()
        ]
        top_similarity = [
            {"signature": signature, "mentions": count}
            for signature, count in similarity_counter.most_common(8)
        ]
        signals_all = theme_signals.get(theme_id, [])
        unaddressed_signals = [item for item in signals_all if not bool(item.get("addressed"))]
        addressed_signals = [item for item in signals_all if bool(item.get("addressed"))]
        themes.append(
            {
                "theme_id": theme_id,
                "title": theme_title_by_id.get(theme_id, theme_id),
                "mentions": mentions,
                "unaddressed_mentions": len(unaddressed_signals),
                "addressed_mentions": len(addressed_signals),
                "runs_citing": len(theme_run_ids.get(theme_id, set())),
                "agents": sorted(theme_agents.get(theme_id, set())),
                "sources": sorted(theme_sources.get(theme_id, set())),
                "top_similarity": top_similarity,
                "similarity_clusters": similarity_clusters,
                "examples": theme_examples.get(theme_id, []),
                "signals": signals_all,
                "unaddressed_signals": unaddressed_signals,
                "addressed_signals": addressed_signals,
            }
        )

    total_issue_mentions = int(sum(issue_counts.values()))
    return {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "action_tracking": actions_meta,
        "totals": {
            "runs": len(records),
            "status_counts": dict(sorted(status_counts.items())),
            "agent_counts": dict(sorted(agent_counts.items())),
            "recommendation_counts": dict(sorted(recommendation_counts.items())),
            "issue_mentions": total_issue_mentions,
            "addressed_issue_mentions": total_addressed_mentions,
            "unaddressed_issue_mentions": total_issue_mentions - total_addressed_mentions,
            "normalization_counts": dict(sorted(normalization_counts.items())),
        },
        "themes": themes,
        "runs": run_summaries,
        "artifacts": {
            "capture_manifest": capture_manifest,
        },
    }


def render_issue_analysis_markdown(
    summary: dict[str, Any],
    *,
    title: str = "Usertest Issue Analysis",
) -> str:
    def _fmt_comment(signal: dict[str, Any], *, include_action: bool) -> str:
        run_dir_raw = signal.get("run_dir")
        source_raw = signal.get("source")
        text_raw = signal.get("text")
        run_dir = run_dir_raw if isinstance(run_dir_raw, str) else "<unknown>"
        source = source_raw if isinstance(source_raw, str) else "unknown"
        text = _format_signal_preview(signal)
        if not text and isinstance(text_raw, str):
            text = text_raw
        if not include_action:
            return f"`{run_dir}` (`{source}`): {text}"

        action_date_raw = signal.get("action_date")
        action_plan_raw = signal.get("action_plan")
        action_id_raw = signal.get("action_id")
        action_date = (
            action_date_raw
            if isinstance(action_date_raw, str) and action_date_raw
            else "unknown"
        )
        action_plan = (
            action_plan_raw
            if isinstance(action_plan_raw, str) and action_plan_raw
            else "unknown"
        )
        action_id = (
            action_id_raw if isinstance(action_id_raw, str) and action_id_raw else "unknown"
        )
        return (
            f"`{run_dir}` (`{source}`): {text} "
            f"[action: `{action_id}`; date: `{action_date}`; plan: `{action_plan}`]"
        )

    totals = summary.get("totals")
    themes = summary.get("themes")
    runs = summary.get("runs")

    totals_dict = totals if isinstance(totals, dict) else {}
    themes_list = themes if isinstance(themes, list) else []
    runs_list = runs if isinstance(runs, list) else []

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    generated = summary.get("generated_at_utc")
    if isinstance(generated, str):
        lines.append(f"Generated: `{generated}`")
        lines.append("")

    lines.append("## Summary")
    lines.append(f"- Total runs: **{totals_dict.get('runs', 0)}**")

    status_counts = totals_dict.get("status_counts")
    if isinstance(status_counts, dict):
        status_items = ", ".join(
            f"**{str(k)}**={int(v)}"
            for k, v in sorted(status_counts.items(), key=lambda item: str(item[0]))
            if isinstance(v, int)
        )
        lines.append(f"- Status counts: {status_items if status_items else 'n/a'}")

    recommendation_counts = totals_dict.get("recommendation_counts")
    if isinstance(recommendation_counts, dict):
        rec_items = ", ".join(
            f"**{str(k)}**={int(v)}"
            for k, v in sorted(recommendation_counts.items(), key=lambda item: str(item[0]))
            if isinstance(v, int)
        )
        lines.append(f"- Adoption recommendations: {rec_items if rec_items else 'n/a'}")

    addressed_mentions = totals_dict.get("addressed_issue_mentions")
    unaddressed_mentions = totals_dict.get("unaddressed_issue_mentions")
    if isinstance(addressed_mentions, int) and isinstance(unaddressed_mentions, int):
        lines.append(
            f"- Issue mentions: unaddressed=**{unaddressed_mentions}**, "
            f"addressed=**{addressed_mentions}**"
        )

    action_tracking = summary.get("action_tracking")
    if isinstance(action_tracking, dict):
        actions_file = action_tracking.get("actions_file")
        loaded_actions = action_tracking.get("loaded_actions")
        skipped_actions = action_tracking.get("skipped_actions")
        if isinstance(actions_file, str) and actions_file:
            lines.append(f"- Action registry: `{actions_file}`")
        if isinstance(loaded_actions, int):
            lines.append(f"- Action rules loaded: **{loaded_actions}**")
        if isinstance(skipped_actions, int):
            lines.append(f"- Action rules skipped: **{skipped_actions}**")

    lines.append("")
    lines.append("## Theme Clusters")

    if not themes_list:
        lines.append("- No issue themes detected.")
        lines.append("")
    else:
        for theme in themes_list:
            if not isinstance(theme, dict):
                continue
            title_raw = theme.get("title")
            theme_title = title_raw if isinstance(title_raw, str) else "Unknown Theme"
            mentions = theme.get("mentions")
            runs_citing = theme.get("runs_citing")
            addressed_count = theme.get("addressed_mentions")
            unaddressed_count = theme.get("unaddressed_mentions")
            lines.append(f"### {theme_title}")
            lines.append(f"- Mentions: **{mentions if isinstance(mentions, int) else 0}**")
            lines.append(f"- Runs citing: **{runs_citing if isinstance(runs_citing, int) else 0}**")
            if isinstance(unaddressed_count, int) and isinstance(addressed_count, int):
                lines.append(
                    f"- Addressing: unaddressed=**{unaddressed_count}**, "
                    f"addressed=**{addressed_count}**"
                )

            agents_raw = theme.get("agents")
            agents = (
                ", ".join(f"`{a}`" for a in agents_raw if isinstance(a, str))
                if isinstance(agents_raw, list)
                else ""
            )
            if agents:
                lines.append(f"- Agents: {agents}")

            sources_raw = theme.get("sources")
            sources = (
                ", ".join(f"`{s}`" for s in sources_raw if isinstance(s, str))
                if isinstance(sources_raw, list)
                else ""
            )
            if sources:
                lines.append(f"- Sources: {sources}")

            clusters_raw = theme.get("similarity_clusters")
            if isinstance(clusters_raw, list) and clusters_raw:
                lines.append("- Similarity analysis (attempted):")
                for item in clusters_raw[:20]:
                    if not isinstance(item, dict):
                        continue
                    signature = item.get("signature")
                    count = item.get("mentions")
                    addressed = item.get("addressed_mentions")
                    unaddressed = item.get("unaddressed_mentions")
                    if not isinstance(signature, str) or not isinstance(count, int):
                        continue
                    if isinstance(unaddressed, int) and isinstance(addressed, int):
                        lines.append(
                            f"  - `{signature}` ({count}; "
                            f"unaddressed={unaddressed}, addressed={addressed})"
                        )
                    else:
                        lines.append(f"  - `{signature}` ({count})")

            unaddressed_raw = theme.get("unaddressed_signals")
            if isinstance(unaddressed_raw, list) and unaddressed_raw:
                lines.append("- Unaddressed comments:")
                for signal in unaddressed_raw:
                    if not isinstance(signal, dict):
                        continue
                    lines.append(f"  - {_fmt_comment(signal, include_action=False)}")

            addressed_raw = theme.get("addressed_signals")
            if isinstance(addressed_raw, list) and addressed_raw:
                lines.append("- Addressed comments (listed after unaddressed):")
                for signal in addressed_raw:
                    if not isinstance(signal, dict):
                        continue
                    lines.append(f"  - {_fmt_comment(signal, include_action=True)}")
            lines.append("")

    lines.append("## Run Index")
    lines.append("")
    lines.append("| Run Dir | Agent | Status | Recommendation | Signals |")
    lines.append("|---|---|---|---|---:|")
    for item in runs_list:
        if not isinstance(item, dict):
            continue
        run_dir = item.get("run_dir")
        agent = item.get("agent")
        status = item.get("status")
        recommendation = item.get("recommendation")
        signals = item.get("issue_signals")
        run_dir_s = run_dir if isinstance(run_dir, str) else ""
        agent_s = agent if isinstance(agent, str) else ""
        status_s = status if isinstance(status, str) else ""
        recommendation_s = recommendation if isinstance(recommendation, str) else ""
        signals_i = signals if isinstance(signals, int) else 0
        lines.append(
            f"| `{run_dir_s}` | `{agent_s}` | `{status_s}` | `{recommendation_s}` | {signals_i} |"
        )
    lines.append("")

    return "\n".join(lines)


def write_issue_analysis(
    summary: dict[str, Any],
    *,
    out_json_path: Path,
    out_md_path: Path,
    title: str = "Usertest Issue Analysis",
) -> None:
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_md_path.parent.mkdir(parents=True, exist_ok=True)

    out_json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md_path.write_text(
        render_issue_analysis_markdown(summary, title=title),
        encoding="utf-8",
    )
