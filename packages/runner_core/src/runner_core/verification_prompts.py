from __future__ import annotations

import json
from typing import Any


def _build_followup_prompt(
    *,
    base_prompt: str,
    report_validation_errors: list[str],
    schema_dict: dict[str, Any],
    prior_last_message_text: str,
    attempt_number: int,
) -> str:
    errors = [str(e).strip() for e in report_validation_errors if str(e).strip()]
    error_block = "\n".join(f"- {line}" for line in errors[:20]) or "- (no error details)"

    prior_message = prior_last_message_text.strip()
    if len(prior_message) > 4000:
        prior_message = prior_message[:4000] + "\n...[truncated]"
    if not prior_message:
        prior_message = "(no prior message captured)"

    schema_json = json.dumps(schema_dict, indent=2, ensure_ascii=False)

    return (
        f"{base_prompt}\n\n"
        "Follow-up required.\n"
        f"This is follow-up attempt #{attempt_number} because your previous response did not "
        "validate against the report schema.\n\n"
        "Validation errors:\n"
        f"{error_block}\n\n"
        "Previous assistant output:\n"
        "```\n"
        f"{prior_message}\n"
        "```\n\n"
        "Return ONLY one JSON object that validates against this schema.\n"
        "Do not include markdown fences, prose, or extra keys.\n\n"
        "Schema:\n"
        f"{schema_json}\n"
    )


def _build_verification_followup_prompt(
    *,
    base_prompt: str,
    verification_summary: dict[str, Any],
    schema_dict: dict[str, Any],
    prior_last_message_text: str,
    attempt_number: int,
) -> str:
    commands = verification_summary.get("commands")
    command_lines: list[str] = []
    if isinstance(commands, list):
        for idx, item in enumerate(commands, start=1):
            if not isinstance(item, dict):
                continue
            cmd = item.get("command")
            exit_code = item.get("exit_code")
            wall_seconds = item.get("wall_seconds")
            timed_out = item.get("timed_out")
            stdout_tail = item.get("stdout_tail")
            stderr_tail = item.get("stderr_tail")
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            command_lines.append(f"{idx}) {cmd.strip()}")
            if isinstance(exit_code, int):
                command_lines.append(f"   exit_code={exit_code}")
            if isinstance(wall_seconds, (int, float)):
                command_lines.append(f"   wall_seconds={wall_seconds:.2f}")
            if isinstance(timed_out, bool):
                command_lines.append(f"   timed_out={str(timed_out).lower()}")
            if isinstance(stdout_tail, str) and stdout_tail.strip():
                command_lines.extend(["   stdout_tail:", "```", stdout_tail.strip(), "```"])
            if isinstance(stderr_tail, str) and stderr_tail.strip():
                command_lines.extend(["   stderr_tail:", "```", stderr_tail.strip(), "```"])

    commands_block = "\n".join(command_lines).strip()
    if not commands_block:
        commands_block = "(no verification command details captured)"

    prior_message = prior_last_message_text.strip()
    if len(prior_message) > 20000:
        prior_message = prior_message[:20000] + "\n...[truncated]"
    if not prior_message:
        prior_message = "(no prior message captured)"

    schema_json = json.dumps(schema_dict, indent=2, ensure_ascii=False)

    artifacts_hint = ""
    artifacts_dir_for_agent = verification_summary.get("artifacts_dir_for_agent")
    if isinstance(artifacts_dir_for_agent, str) and artifacts_dir_for_agent.strip():
        artifacts_hint = f"\n\nVerification artifacts: {artifacts_dir_for_agent.strip()}\n"

    return (
        f"{base_prompt}\n\n"
        "Follow-up required.\n"
        f"This is follow-up attempt #{attempt_number} because the required "
        "verification checks failed.\n\n"
        "Verification results:\n"
        f"{commands_block}"
        f"{artifacts_hint}\n\n"
        "Previous assistant output:\n"
        "```\n"
        f"{prior_message}\n"
        "```\n\n"
        "Fix the issues so the verification checks pass, then return ONLY one JSON object that "
        "validates against this schema.\n"
        "Do not include markdown fences, prose, or extra keys.\n\n"
        "Schema:\n"
        f"{schema_json}\n"
    )


__all__ = (
    "_build_followup_prompt",
    "_build_verification_followup_prompt",
)
