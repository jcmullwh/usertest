from __future__ import annotations

import json
from pathlib import Path

from reporter.analysis import analyze_report_history, render_issue_analysis_markdown


def test_analyze_report_history_clusters_themes(tmp_path: Path) -> None:
    run_ok = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_ok.mkdir(parents=True, exist_ok=True)
    (run_ok / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_ok / "agent_last_message.txt").write_text("", encoding="utf-8")

    run_invalid = tmp_path / "runs" / "target_a" / "20260102T000000Z" / "gemini" / "0"
    run_invalid.mkdir(parents=True, exist_ok=True)
    (run_invalid / "agent_stderr.txt").write_text(
        "Attempt 2 failed with status 429. Retrying with backoff...\n",
        encoding="utf-8",
    )
    (run_invalid / "agent_last_message.txt").write_text(
        "Task complete. I've produced the required JSON output.\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_ok),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "adoption_decision": {"recommendation": "investigate"},
                "confusion_points": [
                    {"summary": "No documentation or examples included in the package."}
                ],
                "suggested_changes": [
                    {"change": "Add __version__ attribute from importlib.metadata."}
                ],
                "confidence_signals": {"missing": ["No entry points are installed"]},
            },
            "report_validation_errors": None,
            "error": None,
        },
        {
            "run_dir": str(run_invalid),
            "run_rel": "target_a/20260102T000000Z/gemini/0",
            "agent": "gemini",
            "status": "report_validation_error",
            "report": None,
            "report_validation_errors": [
                "$: failed to parse JSON from agent output: "
                "Could not find a JSON object in agent output."
            ],
            "error": None,
        },
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    totals = summary["totals"]
    assert totals["runs"] == 2
    assert totals["status_counts"]["ok"] == 1
    assert totals["status_counts"]["report_validation_error"] == 1

    themes = {item["theme_id"]: item for item in summary["themes"]}
    assert "docs_discoverability" in themes
    assert "version_metadata" in themes
    assert "entrypoint_ux" in themes
    assert "output_contract" in themes
    assert "provider_capacity" in themes


def test_render_issue_analysis_markdown_contains_sections() -> None:
    summary = {
        "generated_at_utc": "2026-02-06T00:00:00Z",
        "totals": {
            "runs": 1,
            "status_counts": {"ok": 1},
            "agent_counts": {"codex": 1},
            "recommendation_counts": {"adopt": 1},
            "issue_mentions": 2,
        },
        "themes": [
            {
                "theme_id": "docs_discoverability",
                "title": "Discoverability and Quickstart",
                "mentions": 2,
                "runs_citing": 1,
                "agents": ["codex"],
                "sources": ["confusion_point"],
                "top_similarity": [{"signature": "no documentation examples", "mentions": 2}],
                "examples": [
                    {
                        "run_dir": "runs/usertest/x/codex/0",
                        "agent": "codex",
                        "source": "confusion_point",
                        "text": "No documentation or examples.",
                    }
                ],
            }
        ],
        "runs": [
            {
                "run_dir": "runs/usertest/x/codex/0",
                "agent": "codex",
                "status": "ok",
                "recommendation": "adopt",
                "issue_signals": 2,
            }
        ],
    }

    md = render_issue_analysis_markdown(summary, title="Test Analysis")
    assert "# Test Analysis" in md
    assert "## Theme Clusters" in md
    assert "Discoverability and Quickstart" in md
    assert "## Run Index" in md


def test_analyze_report_history_prefers_error_json_over_duplicate_validation_error(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_dup" / "20260111T000000Z" / "claude" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_dup/20260111T000000Z/claude/0",
            "agent": "claude",
            "status": "report_validation_error",
            "report": None,
            "report_validation_errors": ["claude exited with code 1"],
            "error": {"type": "AgentExecFailed", "message": "claude exited with code 1"},
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    signal_sources = [
        signal.get("source")
        for theme in summary["themes"]
        for signal in theme.get("signals", [])
        if isinstance(signal, dict)
    ]
    assert "run_failure_event" in signal_sources
    assert "error_json" not in signal_sources
    assert "report_validation_error" not in signal_sources


def test_analyze_report_history_captures_large_non_keyword_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_capture" / "20260110T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text(
        "EPIPE writing to socket\n" + ("x" * 220 + "\n") * 5000,
        encoding="utf-8",
    )
    (run_dir / "agent_last_message.txt").write_text(
        "I could not locate the entrypoint.\nTried several commands.\nNeed docs.\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_capture/20260110T000000Z/codex/0",
            "agent": "codex",
            "status": "error",
            "report": None,
            "report_validation_errors": None,
            "error": None,
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    signals = [signal for theme in summary["themes"] for signal in theme["signals"]]
    failure_signal = next(signal for signal in signals if signal["source"] == "run_failure_event")
    attachments = failure_signal["attachments"]
    stderr_attachment = next(item for item in attachments if item["path"] == "agent_stderr.txt")
    assert stderr_attachment["truncated"] is True
    assert "EPIPE writing to socket" in stderr_attachment["excerpt_head"]
    assert stderr_attachment["artifact_ref"]["path"] == "agent_stderr.txt"
    assert stderr_attachment["artifact_ref"]["sha256"]

    last_attachment = next(item for item in attachments if item["path"] == "agent_last_message.txt")
    assert "Tried several commands." in last_attachment["excerpt_head"]

    capture_manifest = summary["artifacts"]["capture_manifest"]
    run_manifest = capture_manifest["target_capture/20260110T000000Z/codex/0"]
    assert any(
        item.get("path") == "agent_stderr.txt" and item.get("truncated") is True
        for item in run_manifest
    )


def test_analyze_report_history_classifies_execution_and_context_signals(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_b" / "20260103T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text(
        "AgentExecFailed: command not allowed by trusted command list\n",
        encoding="utf-8",
    )
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_b/20260103T000000Z/codex/0",
            "agent": "codex",
            "status": "error",
            "report": {
                "confusion_points": [
                    {"summary": "No USERS.md in this workspace root."},
                ],
            },
            "report_validation_errors": None,
            "error": {"error": "RuntimeError"},
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    themes = {item["theme_id"]: item for item in summary["themes"]}
    assert "execution_permissions" in themes
    assert "target_context_contract" in themes
    assert "runtime_process" in themes


def test_analyze_report_history_marks_addressed_comments_with_plan(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_c" / "20260104T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    actions_path = tmp_path / "issue_actions.json"
    actions_path.write_text(
        json.dumps(
            {
                "version": 1,
                "actions": [
                    {
                        "id": "a-docs",
                        "date": "2026-02-07",
                        "plan": "docs/ops/demo_plan.md",
                        "match": {
                            "theme_ids": ["docs_discoverability"],
                            "text_patterns": ["no documentation"],
                        },
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_c/20260104T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "confusion_points": [
                    {"summary": "No documentation is available for this flow."},
                    {"summary": "Package naming is unclear for first-time users."},
                ],
            },
            "report_validation_errors": None,
            "error": None,
        }
    ]

    summary = analyze_report_history(
        records,
        repo_root=tmp_path,
        issue_actions_path=actions_path,
    )
    totals = summary["totals"]
    assert totals["addressed_issue_mentions"] >= 1
    assert totals["unaddressed_issue_mentions"] >= 1

    themes = {item["theme_id"]: item for item in summary["themes"]}
    docs_theme = themes["docs_discoverability"]
    assert docs_theme["addressed_mentions"] == 1
    assert docs_theme["unaddressed_mentions"] >= 0
    assert len(docs_theme["addressed_signals"]) == 1
    assert docs_theme["addressed_signals"][0]["action_plan"] == "docs/ops/demo_plan.md"

    md = render_issue_analysis_markdown(summary, title="Addressed Demo")
    assert "Addressed comments (listed after unaddressed)" in md
    assert "docs/ops/demo_plan.md" in md


def test_analyze_report_history_keeps_all_uncategorized_signals(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_d" / "20260105T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_d/20260105T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": {
                "confusion_points": [
                    {"summary": "Opaque edge-case behavior around fallback ordering."},
                    {"summary": "Ambiguous lifecycle ownership for generated artifacts."},
                ],
            },
            "report_validation_errors": None,
            "error": None,
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    other_theme = next(item for item in summary["themes"] if item["theme_id"] == "other")
    assert len(other_theme["signals"]) >= 2
    confusion_signals = [
        signal for signal in other_theme["signals"] if signal.get("source") == "confusion_point"
    ]
    assert len(confusion_signals) == 2
    assert len(other_theme["unaddressed_signals"]) == len(other_theme["signals"])


def test_analyze_report_history_classifies_nullable_path_schema_error(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_e" / "20260106T000000Z" / "gemini" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_e/20260106T000000Z/gemini/0",
            "agent": "gemini",
            "status": "report_validation_error",
            "report": None,
            "report_validation_errors": [
                "$.outputs[0].path: None is not of type 'string'",
            ],
            "error": None,
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    theme_ids = {item["theme_id"] for item in summary["themes"]}
    assert "output_contract" in theme_ids


def test_analyze_report_history_preserves_raw_text_and_normalizes_json_wrapper(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_f" / "20260107T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")

    last_message_obj = {
        "schema_version": 1,
        "persona": {"name": "Quickstart Sprinter"},
        "mission": "Complete Output (Smoke)",
        "adoption_decision": {"recommendation": "investigate"},
        "confusion_points": [{"summary": "No documentation is available."}],
        "suggested_changes": [{"change": "Add a simple wrapper script."}],
        "confidence_signals": {"missing": ["Single-command quickstart."]},
    }
    last_message_text = json.dumps(last_message_obj, ensure_ascii=False)
    (run_dir / "agent_last_message.txt").write_text(last_message_text, encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_f/20260107T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": None,
            "report_validation_errors": None,
            "error": None,
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    totals = summary["totals"]
    normalization_counts = totals["normalization_counts"]
    assert normalization_counts["report_json_envelope"] == 1
    theme_ids = {item["theme_id"] for item in summary["themes"]}
    assert "output_envelope" in theme_ids

    signals = [
        signal
        for theme in summary["themes"]
        for signal in theme["signals"]
        if signal.get("source") == "agent_last_message"
    ]
    assert len(signals) == 1
    signal = signals[0]
    assert signal["raw_text"] == last_message_text
    assert signal["normalization_kind"] == "report_json_envelope"
    assert "report_json_envelope" in signal["normalized_text"]
    assert "report_json_envelope" in signal["signature"]


def test_analyze_report_history_extracts_json_wrapper_from_prose_with_fenced_block(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_g" / "20260108T000000Z" / "codex" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")

    last_message_obj = {
        "schema_version": 1,
        "persona": {"name": "Quickstart Sprinter"},
        "mission": "Complete Output (Smoke)",
        "adoption_decision": {"recommendation": "investigate"},
        "confusion_points": [{"summary": "No documentation is available."}],
        "suggested_changes": [{"change": "Add a simple wrapper script."}],
        "confidence_signals": {"missing": ["Single-command quickstart."]},
    }
    report_json = json.dumps(last_message_obj, indent=2, ensure_ascii=False)
    last_message_text = (
        "Here is my report.\n\n"
        "```json\n"
        f"{report_json}\n"
        "```\n"
    )
    (run_dir / "agent_last_message.txt").write_text(last_message_text, encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_g/20260108T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "report": None,
            "report_validation_errors": None,
            "error": None,
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    normalization_counts = summary["totals"]["normalization_counts"]
    assert normalization_counts["report_json_envelope"] == 1


def test_render_issue_analysis_markdown_truncates_preview_but_keeps_signal_pointer() -> None:
    long_text = "x" * 700
    summary = {
        "generated_at_utc": "2026-02-07T00:00:00Z",
        "totals": {
            "runs": 1,
            "status_counts": {"ok": 1},
            "agent_counts": {"codex": 1},
            "recommendation_counts": {"investigate": 1},
            "issue_mentions": 1,
            "addressed_issue_mentions": 0,
            "unaddressed_issue_mentions": 1,
            "normalization_counts": {},
        },
        "themes": [
            {
                "theme_id": "other",
                "title": "Other / Unclassified",
                "mentions": 1,
                "runs_citing": 1,
                "agents": ["codex"],
                "sources": ["agent_last_message"],
                "top_similarity": [{"signature": "other", "mentions": 1}],
                "similarity_clusters": [
                    {
                        "signature": "other",
                        "mentions": 1,
                        "unaddressed_mentions": 1,
                        "addressed_mentions": 0,
                    }
                ],
                "examples": [],
                "signals": [
                    {
                        "run_dir": "runs/usertest/target/x/codex/0",
                        "run_id": "target/x/codex/0",
                        "signal_id": "target/x/codex/0:1",
                        "agent": "codex",
                        "source": "agent_last_message",
                        "signature": "other",
                        "text": long_text,
                        "raw_text": long_text,
                        "normalized_text": long_text,
                        "normalization_kind": None,
                        "addressed": False,
                    }
                ],
                "unaddressed_signals": [
                    {
                        "run_dir": "runs/usertest/target/x/codex/0",
                        "run_id": "target/x/codex/0",
                        "signal_id": "target/x/codex/0:1",
                        "agent": "codex",
                        "source": "agent_last_message",
                        "signature": "other",
                        "text": long_text,
                        "raw_text": long_text,
                        "normalized_text": long_text,
                        "normalization_kind": None,
                        "addressed": False,
                    }
                ],
                "addressed_signals": [],
            }
        ],
        "runs": [
            {
                "run_dir": "runs/usertest/target/x/codex/0",
                "agent": "codex",
                "status": "ok",
                "recommendation": "investigate",
                "issue_signals": 1,
            }
        ],
    }

    md = render_issue_analysis_markdown(summary, title="Preview Test")
    assert "truncated; see `raw_text` in JSON" in md
    assert "signal_id: `target/x/codex/0:1`" in md
    assert ("x" * 500) not in md


def test_analyze_report_history_classifies_tool_registry_and_policy_denial(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "target_g" / "20260108T000000Z" / "gemini" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text(
        "\n".join(
            [
                "Error executing tool write_todos: Tool execution denied by policy.",
                (
                    'Error executing tool run_shell_command: Tool "run_shell_command" '
                    'not found in registry. Tools must use the exact names that are registered.'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_g/20260108T000000Z/gemini/0",
            "agent": "gemini",
            "status": "ok",
            "report": None,
            "report_validation_errors": None,
            "error": None,
        }
    ]
    summary = analyze_report_history(records, repo_root=tmp_path)
    theme_ids = {item["theme_id"] for item in summary["themes"]}
    assert "execution_permissions" in theme_ids


def test_analyze_report_history_classifies_limit_message_as_provider_capacity(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target_h" / "20260109T000000Z" / "claude" / "0"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text(
        "You've hit your limit · resets 1pm (America/New_York)\n",
        encoding="utf-8",
    )

    records = [
        {
            "run_dir": str(run_dir),
            "run_rel": "target_h/20260109T000000Z/claude/0",
            "agent": "claude",
            "status": "error",
            "report": None,
            "report_validation_errors": ["claude exited with code 1"],
            "error": {"type": "AgentExecFailed", "exit_code": 1},
        }
    ]

    summary = analyze_report_history(records, repo_root=tmp_path)
    theme_ids = {item["theme_id"] for item in summary["themes"]}
    assert "provider_capacity" in theme_ids
