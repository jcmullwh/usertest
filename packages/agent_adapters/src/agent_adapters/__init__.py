from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from agent_adapters.claude_cli import ClaudePrintResult, run_claude_print
from agent_adapters.claude_normalize import normalize_claude_events
from agent_adapters.codex_cli import (
    CodexExecResult,
    CodexPersonalityConfigIssue,
    CodexReasoningEffortConfigIssue,
    run_codex_exec,
    validate_codex_personality_config_overrides,
    validate_codex_reasoning_effort_config_overrides,
)
from agent_adapters.codex_normalize import normalize_codex_events
from agent_adapters.gemini_cli import GeminiRunResult, run_gemini
from agent_adapters.gemini_normalize import normalize_gemini_events


def _resolve_version() -> str:
    for distribution_name in ("agent-adapters", "agent_adapters"):
        try:
            return package_version(distribution_name)
        except PackageNotFoundError:
            continue
    return "0+unknown"


__version__ = _resolve_version()

__all__ = [
    "__version__",
    "ClaudePrintResult",
    "CodexExecResult",
    "CodexPersonalityConfigIssue",
    "CodexReasoningEffortConfigIssue",
    "GeminiRunResult",
    "normalize_claude_events",
    "normalize_codex_events",
    "normalize_gemini_events",
    "run_claude_print",
    "run_codex_exec",
    "validate_codex_personality_config_overrides",
    "validate_codex_reasoning_effort_config_overrides",
    "run_gemini",
]
