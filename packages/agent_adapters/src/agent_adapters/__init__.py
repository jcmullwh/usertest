from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from agent_adapters.claude_cli import ClaudePrintResult, run_claude_print
from agent_adapters.claude_normalize import normalize_claude_events
from agent_adapters.codex_cli import (
    CODEX_CHATGPT_SUBSCRIPTION_BASE_URL,
    CODEX_OPENAI_SUBSCRIPTION_BASE_URL,
    CODEX_SUBSCRIPTION_AUTH_ENV_VARS,
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_PROVIDER_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    CodexExecResult,
    CodexLoginStatusResult,
    CodexPersonalityConfigIssue,
    CodexReasoningEffortConfigIssue,
    build_codex_subscription_config_overrides,
    codex_subscription_config_errors,
    probe_codex_login_status,
    resolve_codex_executable,
    run_codex_exec,
    validate_codex_personality_config_overrides,
    validate_codex_reasoning_effort_config_overrides,
    validate_codex_subscription_config_overrides,
)
from agent_adapters.codex_normalize import normalize_codex_events
from agent_adapters.gemini_cli import GeminiRunResult, run_gemini
from agent_adapters.gemini_normalize import normalize_gemini_events
from agent_adapters.shell_probe import AgentShellProbeResult, probe_agent_shell_launch


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
    "CODEX_CHATGPT_SUBSCRIPTION_BASE_URL",
    "CODEX_OPENAI_SUBSCRIPTION_BASE_URL",
    "CODEX_SUBSCRIPTION_AUTH_ENV_VARS",
    "CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS",
    "CODEX_SUBSCRIPTION_PROVIDER_ENV_VARS",
    "CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES",
    "CodexExecResult",
    "CodexLoginStatusResult",
    "CodexPersonalityConfigIssue",
    "CodexReasoningEffortConfigIssue",
    "GeminiRunResult",
    "build_codex_subscription_config_overrides",
    "codex_subscription_config_errors",
    "AgentShellProbeResult",
    "normalize_claude_events",
    "normalize_codex_events",
    "normalize_gemini_events",
    "probe_agent_shell_launch",
    "probe_codex_login_status",
    "resolve_codex_executable",
    "run_claude_print",
    "run_codex_exec",
    "validate_codex_personality_config_overrides",
    "validate_codex_reasoning_effort_config_overrides",
    "validate_codex_subscription_config_overrides",
    "run_gemini",
]
