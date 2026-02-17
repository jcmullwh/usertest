from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ENVELOPE_SIGNATURE_RE = re.compile(
    r"^agent_last_message (?:json_object_keys|report_json_envelope|json_array_len)\b",
    re.IGNORECASE,
)
_FENCED_JSON_RE = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
_JSON_KV_STR_RE = re.compile(r'^\s*"?([A-Za-z0-9_]+)"?\s*:\s*"([^"]+)"\s*,?\s*$')
_JSON_KV_NUM_RE = re.compile(r'^\s*"?([A-Za-z0-9_]+)"?\s*:\s*([0-9]+)\s*,?\s*$')


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_ws(text: str) -> str:
    return " ".join(text.replace("\r", "\n").split())


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _sanitize_line(text: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None

    if candidate.startswith("[json-envelope-comment]"):
        candidate = candidate[len("[json-envelope-comment]") :].strip()
    if _ENVELOPE_SIGNATURE_RE.match(candidate):
        return None
    if "\\n' +" in candidate or candidate.endswith("' +"):
        return None
    if candidate.startswith("{") and candidate.endswith("}"):
        return None
    if "\\n" in candidate and candidate.count("\\n") > 3:
        return None

    kv_str = _JSON_KV_STR_RE.match(candidate)
    if kv_str:
        key, value = kv_str.groups()
        key_l = key.lower()
        if key_l in {"code", "line", "col"}:
            return None
        value_clean = _normalize_ws(value)
        if not value_clean:
            return None
        if key_l in {"message", "reason", "status", "title", "summary", "details"}:
            return value_clean
        return f"{key}: {value_clean}"

    if _JSON_KV_NUM_RE.match(candidate):
        return None

    if (
        len(candidate) >= 2
        and candidate[0] == candidate[-1]
        and candidate[0] in {"'", '"'}
    ):
        candidate = candidate[1:-1].strip()

    candidate = _normalize_ws(candidate)
    if not candidate:
        return None
    if len(candidate) < 12:
        return None
    if _ENVELOPE_SIGNATURE_RE.match(candidate):
        return None
    return candidate


def _try_parse_json(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return None
    try:
        return json.loads(stripped)
    except Exception:  # noqa: BLE001
        return None


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    direct = _try_parse_json(text)
    if isinstance(direct, dict):
        out.append(direct)

    for match in _FENCED_JSON_RE.finditer(text):
        parsed = _try_parse_json(match.group(1))
        if isinstance(parsed, dict):
            out.append(parsed)

    return out


def _extract_report_feedback(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []

    confusion = payload.get("confusion_points")
    if isinstance(confusion, list):
        for item in confusion:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary")
            if isinstance(summary, str):
                cleaned = _sanitize_line(summary)
                if cleaned:
                    out.append(cleaned)

    suggested = payload.get("suggested_changes")
    if isinstance(suggested, list):
        for item in suggested:
            if not isinstance(item, dict):
                continue
            change = item.get("change")
            if isinstance(change, str):
                cleaned = _sanitize_line(change)
                if cleaned:
                    out.append(cleaned)

    confidence = payload.get("confidence_signals")
    if isinstance(confidence, dict):
        missing = confidence.get("missing")
        if isinstance(missing, list):
            for value in missing:
                if isinstance(value, str):
                    cleaned = _sanitize_line(value)
                    if cleaned:
                        out.append(cleaned)

    return out


def _extract_generic_feedback(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []

    issues = payload.get("issues")
    if isinstance(issues, list):
        for item in issues:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            details = item.get("details")
            if isinstance(title, str) and isinstance(details, str):
                cleaned = _sanitize_line(f"{title}: {details}")
                if cleaned:
                    out.append(cleaned)
            elif isinstance(details, str):
                cleaned = _sanitize_line(details)
                if cleaned:
                    out.append(cleaned)
            elif isinstance(title, str):
                cleaned = _sanitize_line(title)
                if cleaned:
                    out.append(cleaned)

    risks = payload.get("risks")
    if isinstance(risks, list):
        for item in risks:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            details = item.get("details")
            if isinstance(title, str) and isinstance(details, str):
                cleaned = _sanitize_line(f"{title}: {details}")
                if cleaned:
                    out.append(cleaned)
            elif isinstance(details, str):
                cleaned = _sanitize_line(details)
                if cleaned:
                    out.append(cleaned)
            elif isinstance(title, str):
                cleaned = _sanitize_line(title)
                if cleaned:
                    out.append(cleaned)

    observations = payload.get("observations")
    if isinstance(observations, list):
        for item in observations:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary")
            if isinstance(summary, str):
                cleaned = _sanitize_line(summary)
                if cleaned:
                    out.append(cleaned)

    recommendations = payload.get("recommendations")
    if isinstance(recommendations, list):
        for rec in recommendations:
            if isinstance(rec, str):
                cleaned = _sanitize_line(rec)
                if cleaned:
                    out.append(cleaned)

    next_actions = payload.get("next_actions")
    if isinstance(next_actions, list):
        for action in next_actions:
            if isinstance(action, str):
                cleaned = _sanitize_line(action)
                if cleaned:
                    out.append(cleaned)

    failure_point = payload.get("failure_point")
    if isinstance(failure_point, str):
        cleaned = _sanitize_line(failure_point)
        if cleaned:
            out.append(cleaned)

    return out


def _extract_comments_from_text(*, source: str, text: str) -> list[str]:
    out: list[str] = []
    parsed_objs = _extract_json_objects(text)

    if source == "agent_last_message":
        for payload in parsed_objs:
            out.extend(_extract_report_feedback(payload))
            out.extend(_extract_generic_feedback(payload))

        if "```" in text:
            prefix = text.split("```", 1)[0].strip()
            if prefix:
                cleaned = _sanitize_line(prefix)
                if cleaned:
                    out.append(cleaned)
        if not parsed_objs:
            cleaned_direct = _sanitize_line(text)
            if cleaned_direct:
                out.append(cleaned_direct)
    else:
        if parsed_objs:
            for payload in parsed_objs:
                out.extend(_extract_report_feedback(payload))
                out.extend(_extract_generic_feedback(payload))
        cleaned_direct = _sanitize_line(text)
        if cleaned_direct:
            out.append(cleaned_direct)

    return _dedupe(out)


def _build_signal_index(mentions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    entries = mentions.get("mentions")
    if not isinstance(entries, list):
        return out
    for item in entries:
        if not isinstance(item, dict):
            continue
        signal_id = item.get("signal_id")
        if isinstance(signal_id, str) and signal_id.strip():
            out[signal_id] = item
    return out


def _build_rank_index(backlog: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    clusters = backlog.get("clusters")
    if not isinstance(clusters, list):
        return out
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        rank = cluster.get("rank")
        if isinstance(rank, int):
            out[rank] = cluster
    return out


def _render_markdown(doc: dict[str, Any]) -> str:
    totals = doc.get("totals")
    totals_dict = totals if isinstance(totals, dict) else {}
    feedback = doc.get("feedback")
    feedback_list = feedback if isinstance(feedback, list) else []

    lines: list[str] = []
    lines.append("# Human-Readable Operational Feedback (Iterations 1-7)")
    lines.append("")
    lines.append(f"Generated: `{doc.get('generated_at_utc', '')}`")
    lines.append(f"- Input: `{doc.get('input', '')}`")
    lines.append(f"- Feedback items: **{totals_dict.get('feedback_items', 0)}**")
    lines.append(f"- Feedback mentions covered: **{totals_dict.get('feedback_mentions', 0)}**")
    lines.append(
        "- Dropped non-informational artifact clusters: "
        f"**{totals_dict.get('artifact_clusters_dropped', 0)}** "
        f"(mentions: **{totals_dict.get('artifact_mentions_dropped', 0)}**)"
    )
    lines.append(
        "- Note: Raw comments below are extracted human feedback text only; "
        "metadata signatures and JSON key lists are excluded."
    )
    lines.append("")
    lines.append("## Ranked Feedback")

    for idx, item in enumerate(feedback_list, start=1):
        if not isinstance(item, dict):
            continue
        theme_id = item.get("theme_id", "unknown")
        mentions = item.get("mentions", 0)
        runs = item.get("runs_citing", 0)
        lines.append(f"## {idx}. `{theme_id}` | mentions={mentions} | runs={runs}")
        lines.append(f"- Feedback: {item.get('feedback', '')}")
        lines.append(f"- Owner: `{item.get('suggested_owner', 'unknown')}`")

        candidate_files = item.get("candidate_files")
        if isinstance(candidate_files, list):
            rendered = ", ".join(f"`{p}`" for p in candidate_files if isinstance(p, str))
            lines.append(f"- Candidate files: {rendered if rendered else 'n/a'}")
        else:
            lines.append("- Candidate files: n/a")

        agents = item.get("agents")
        if isinstance(agents, list):
            rendered_agents = ", ".join(f"`{a}`" for a in agents if isinstance(a, str))
            lines.append(f"- Agents: {rendered_agents if rendered_agents else 'n/a'}")
        else:
            lines.append("- Agents: n/a")

        lines.append("- Raw comments used:")
        raw_comments = item.get("raw_comments")
        if isinstance(raw_comments, list) and raw_comments:
            for raw in raw_comments:
                if isinstance(raw, str):
                    lines.append(f"  - {raw}")
        else:
            lines.append("  - [none extracted]")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def rebuild_raw_comments(
    *,
    feedback_doc: dict[str, Any],
    backlog_doc: dict[str, Any],
    mentions_doc: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(feedback_doc)
    updated["generated_at_utc"] = _utc_now()
    updated["input"] = str(backlog_doc.get("input") or updated.get("input") or "")

    signal_index = _build_signal_index(mentions_doc)
    rank_index = _build_rank_index(backlog_doc)

    feedback_raw = updated.get("feedback")
    feedback = feedback_raw if isinstance(feedback_raw, list) else []
    out_feedback: list[dict[str, Any]] = []
    for entry in feedback:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        extracted: list[str] = []

        ranks = item.get("cluster_ranks")
        rank_list = [r for r in ranks if isinstance(r, int)] if isinstance(ranks, list) else []
        for rank in rank_list:
            cluster = rank_index.get(rank)
            if not isinstance(cluster, dict):
                continue

            cluster_comments: list[str] = []
            signal_ids = cluster.get("signal_ids")
            if isinstance(signal_ids, list):
                for signal_id in signal_ids:
                    if not isinstance(signal_id, str):
                        continue
                    mention = signal_index.get(signal_id)
                    if not isinstance(mention, dict):
                        continue
                    source_raw = mention.get("source")
                    text_raw = mention.get("raw_text")
                    source = source_raw if isinstance(source_raw, str) else "unknown"
                    text = text_raw if isinstance(text_raw, str) else ""
                    cluster_comments.extend(
                        _extract_comments_from_text(source=source, text=text)
                    )

            if not cluster_comments:
                examples = cluster.get("examples")
                if isinstance(examples, list):
                    for ex in examples:
                        if isinstance(ex, str):
                            cluster_comments.extend(
                                _extract_comments_from_text(source="unknown", text=ex)
                            )

            extracted.extend(cluster_comments)

        item["raw_comments"] = _dedupe(extracted)
        out_feedback.append(item)

    updated["feedback"] = out_feedback
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regenerate human-readable operational feedback with strict raw comment extraction."
    )
    parser.add_argument("--feedback-json", type=Path, required=True)
    parser.add_argument("--backlog-json", type=Path, required=True)
    parser.add_argument("--mentions-json", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    feedback_path: Path = args.feedback_json
    backlog_path: Path = args.backlog_json
    mentions_path: Path = args.mentions_json

    out_json = args.out_json if isinstance(args.out_json, Path) else feedback_path
    out_md = args.out_md if isinstance(args.out_md, Path) else feedback_path.with_suffix(".md")

    feedback_doc = _load_json(feedback_path)
    backlog_doc = _load_json(backlog_path)
    mentions_doc = _load_json(mentions_path)

    if not isinstance(feedback_doc, dict):
        raise ValueError(f"Expected dict JSON in {feedback_path}")
    if not isinstance(backlog_doc, dict):
        raise ValueError(f"Expected dict JSON in {backlog_path}")
    if not isinstance(mentions_doc, dict):
        raise ValueError(f"Expected dict JSON in {mentions_path}")

    rebuilt = rebuild_raw_comments(
        feedback_doc=feedback_doc,
        backlog_doc=backlog_doc,
        mentions_doc=mentions_doc,
    )
    rendered = _render_markdown(rebuilt)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(rebuilt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md.write_text(rendered, encoding="utf-8")

    totals = rebuilt.get("totals")
    feedback = rebuilt.get("feedback")
    feedback_count = len(feedback) if isinstance(feedback, list) else 0
    print(f"Wrote JSON: {out_json}")
    print(f"Wrote MD:   {out_md}")
    if isinstance(totals, dict):
        print(
            "Summary: "
            f"feedback_items={totals.get('feedback_items')} "
            f"feedback_mentions={totals.get('feedback_mentions')} "
            f"artifact_clusters_dropped={totals.get('artifact_clusters_dropped')}"
        )
    print(f"Feedback entries rendered: {feedback_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
