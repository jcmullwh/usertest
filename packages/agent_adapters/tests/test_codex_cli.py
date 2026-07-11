from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_adapters.codex_cli import (
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    CodexLoginStatusResult,
    _resolve_executable,
    build_codex_subscription_config_overrides,
    codex_subscription_config_errors,
    probe_codex_login_status,
    run_codex_exec,
    validate_codex_personality_config_overrides,
    validate_codex_reasoning_effort_config_overrides,
    validate_codex_subscription_config_overrides,
)


def _make_login_status_dummy_codex(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_login_status.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "capture = Path(os.environ['CODEX_STATUS_CAPTURE'])",
                "capture.write_text(json.dumps({",
                "    'argv': sys.argv[1:],",
                "    'CODEX_HOME': os.environ.get('CODEX_HOME'),",
                "    'OPENAI_API_KEY': os.environ.get('OPENAI_API_KEY'),",
                "    'CODEX_API_KEY': os.environ.get('CODEX_API_KEY'),",
                "    'CODEX_ACCESS_TOKEN': os.environ.get('CODEX_ACCESS_TOKEN'),",
                "    'OPENAI_BASE_URL': os.environ.get('OPENAI_BASE_URL'),",
                "    'OPENAI_API_BASE': os.environ.get('OPENAI_API_BASE'),",
                "    'OPENAI_ORG_ID': os.environ.get('OPENAI_ORG_ID'),",
                "    'OPENAI_ORGANIZATION': os.environ.get('OPENAI_ORGANIZATION'),",
                "}), encoding='utf-8')",
                "print(os.environ.get('CODEX_STATUS_STDOUT', 'Logged in using ChatGPT'))",
                "sys.stderr.write(os.environ.get('CODEX_STATUS_STDERR', ''))",
                "raise SystemExit(int(os.environ.get('CODEX_STATUS_EXIT', '0')))",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    if os.name == "nt":
        wrapper = tmp_path / "dummy_codex_login_status.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
        return str(wrapper)
    wrapper = tmp_path / "dummy_codex_login_status.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def _make_refresh_token_reused_dummy_codex(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_refresh_token_reused.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                "import time",
                "",
                "",
                "def main() -> None:",
                "    try:",
                "        sys.stdin.read()",
                "    except Exception:",
                "        pass",
                "",
                "    while True:",
                "        sys.stderr.write(",
                "            'ERROR codex_core::auth: Failed to refresh token: 401 Unauthorized: '",
                '            \'{"error": {"code": "refresh_token_reused"}}\\n\'',
                "        )",
                "        sys.stderr.flush()",
                "        time.sleep(0.05)",
                "",
                "",
                "if __name__ == '__main__':",
                "    main()",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )

    if os.name == "nt":
        wrapper = tmp_path / "dummy_codex_refresh_token_reused.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
        return str(wrapper)

    wrapper = tmp_path / "dummy_codex_refresh_token_reused.sh"
    wrapper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f'"{sys.executable}" "{script}" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def _make_argv_dump_dummy_codex(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_argv_dump.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import sys",
                "",
                "",
                "def main() -> None:",
                "    sys.stdin.read()",
                "    out = os.environ['CODEX_ARGV_OUT']",
                "    with open(out, 'w', encoding='utf-8', newline='\\n') as f:",
                "        json.dump(sys.argv[1:], f)",
                "",
                "",
                "if __name__ == '__main__':",
                "    main()",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )

    if os.name == "nt":
        wrapper = tmp_path / "dummy_codex_argv_dump.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
        return str(wrapper)

    wrapper = tmp_path / "dummy_codex_argv_dump.sh"
    wrapper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f'"{sys.executable}" "{script}" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PATH resolution for .cmd files")
def test_resolve_executable_finds_cmd_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = tmp_path / "dummy.cmd"
    cmd.write_text("@echo off\necho dummy_ok\n", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("PATHEXT", f"{os.environ.get('PATHEXT', '')};.CMD")

    resolved = _resolve_executable("dummy")
    assert Path(resolved).resolve() == cmd.resolve()

    proc = subprocess.run([resolved], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "dummy_ok" in proc.stdout


def test_probe_codex_login_status_uses_direct_host_home_and_blanks_alternate_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _make_login_status_dummy_codex(tmp_path)
    capture = tmp_path / "status-capture.json"
    host_codex_home = tmp_path / "host-codex-home"
    host_codex_home.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "billable-key")
    monkeypatch.setenv("CODEX_API_KEY", "alternate-billable-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "alternate-token")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://billable.invalid/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://legacy.invalid/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-id")
    monkeypatch.setenv("OPENAI_ORGANIZATION", "organization-id")

    result = probe_codex_login_status(
        binary=binary,
        codex_home=host_codex_home,
        cwd=tmp_path,
        config_overrides=(
            'forced_login_method="chatgpt"',
            'model_provider="openai"',
        ),
        env_overrides={
            "CODEX_STATUS_CAPTURE": str(capture),
        },
    )

    assert result.ok is True
    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert Path(observed["CODEX_HOME"]).resolve() == host_codex_home.resolve()
    assert observed["OPENAI_API_KEY"] == ""
    assert observed["CODEX_API_KEY"] == ""
    assert observed["CODEX_ACCESS_TOKEN"] == ""
    assert observed["OPENAI_BASE_URL"] == ""
    assert observed["OPENAI_API_BASE"] == ""
    assert observed["OPENAI_ORG_ID"] == ""
    assert observed["OPENAI_ORGANIZATION"] == ""
    assert observed["argv"][-2:] == ["login", "status"]
    assert 'forced_login_method="chatgpt"' in observed["argv"]
    assert 'model_provider="openai"' in observed["argv"]
    assert observed["argv"].index('forced_login_method="chatgpt"') < observed["argv"].index("login")
    redacted = result.to_redacted_dict()
    assert redacted["status_kind"] == "chatgpt"


def test_codex_login_status_accepts_exact_chatgpt_status_on_stderr() -> None:
    result = CodexLoginStatusResult(
        argv=["codex", "login", "status"],
        exit_code=0,
        stdout="",
        stderr="Logged in using ChatGPT\n",
        codex_home="C:/host/.codex",
        auth_env_vars_blank={name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    )

    assert result.ok is True
    assert result.to_redacted_dict()["status_kind"] == "chatgpt"


def test_subscription_config_preserves_safe_knobs_and_forces_canonical_route_last() -> None:
    safe = [
        "model_reasoning_effort=high",
        'model_instructions_file="C:/run/instructions.md"',
        "features.plugins=false",
    ]

    effective = build_codex_subscription_config_overrides(
        safe,
        source="test",
    )

    assert effective[: len(safe)] == safe
    assert effective[-len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES) :] == list(
        CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES
    )
    assert codex_subscription_config_errors(effective) == []


@pytest.mark.parametrize(
    "override",
    [
        'chatgpt_base_url="https://billable.invalid"',
        'openai_base_url="https://billable.invalid/v1"',
        'model_provider="custom"',
        'model_providers.custom.base_url="https://billable.invalid/v1"',
        'profile="billable"',
        'profiles.billable.chatgpt_base_url="https://billable.invalid"',
        'cli_auth_credentials_store="ephemeral"',
        'provider_token="secret"',
        'debug.config_lockfile.load_path="C:/tmp/alternate-config.json"',
    ],
)
def test_subscription_config_rejects_routing_profile_and_credential_overrides(
    override: str,
) -> None:
    with pytest.raises(ValueError, match="codex_subscription_config_override_forbidden"):
        validate_codex_subscription_config_overrides([override], source="test")


def test_subscription_config_detects_canonical_route_tampering() -> None:
    effective = build_codex_subscription_config_overrides(
        ["model_reasoning_effort=high"],
        source="test",
    )
    effective[-4] = 'chatgpt_base_url="https://billable.invalid"'

    errors = codex_subscription_config_errors(effective)

    assert "codex_subscription_canonical_route_suffix_missing" in errors
    assert any("codex_subscription_config_override_forbidden" in error for error in errors)


@pytest.mark.parametrize(
    ("stdout", "exit_code", "expected_kind"),
    [
        ("Logged in using an API key", 0, "api_key"),
        ("unexpected status payload", 0, "unexpected"),
        ("", 7, "missing"),
    ],
)
def test_probe_codex_login_status_rejects_non_chatgpt_results(
    tmp_path: Path,
    stdout: str,
    exit_code: int,
    expected_kind: str,
) -> None:
    binary = _make_login_status_dummy_codex(tmp_path)
    capture = tmp_path / "status-capture.json"
    host_codex_home = tmp_path / "host-codex-home"
    host_codex_home.mkdir()

    result = probe_codex_login_status(
        binary=binary,
        codex_home=host_codex_home,
        cwd=tmp_path,
        env_overrides={
            "CODEX_STATUS_CAPTURE": str(capture),
            "CODEX_STATUS_STDOUT": stdout,
            "CODEX_STATUS_EXIT": str(exit_code),
        },
    )

    assert result.ok is False
    assert result.to_redacted_dict()["status_kind"] == expected_kind


def test_run_codex_exec_fails_fast_on_refresh_token_reused(tmp_path: Path) -> None:
    dummy_binary = _make_refresh_token_reused_dummy_codex(tmp_path)

    stderr_path = tmp_path / "stderr.txt"
    raw_events_path = tmp_path / "raw_events.jsonl"
    last_message_path = tmp_path / "last_message.txt"

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="test",
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        sandbox="read-only",
        ask_for_approval="never",
        binary=dummy_binary,
        timeout_seconds=1.0,
    )

    assert result.exit_code != 0
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "Codex authentication failed: refresh_token_reused" in stderr_text
    assert "codex logout" in stderr_text
    assert "codex login" in stderr_text


def test_run_codex_exec_ignores_user_config_for_headless_runs(tmp_path: Path) -> None:
    dummy_binary = _make_argv_dump_dummy_codex(tmp_path)
    argv_path = tmp_path / "argv.json"

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="test",
        raw_events_path=tmp_path / "raw_events.jsonl",
        last_message_path=tmp_path / "last_message.txt",
        stderr_path=tmp_path / "stderr.txt",
        sandbox="read-only",
        ask_for_approval="never",
        binary=dummy_binary,
        env_overrides={"CODEX_ARGV_OUT": str(argv_path)},
    )

    assert result.exit_code == 0
    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    assert argv[argv.index("exec") + 1] == "--ignore-user-config"


def test_run_codex_exec_can_ignore_rules_for_isolated_runs(tmp_path: Path) -> None:
    dummy_binary = _make_argv_dump_dummy_codex(tmp_path)
    argv_path = tmp_path / "argv.json"

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="test",
        raw_events_path=tmp_path / "raw_events.jsonl",
        last_message_path=tmp_path / "last_message.txt",
        stderr_path=tmp_path / "stderr.txt",
        sandbox="read-only",
        ask_for_approval="never",
        binary=dummy_binary,
        ignore_rules=True,
        env_overrides={"CODEX_ARGV_OUT": str(argv_path)},
    )

    assert result.exit_code == 0
    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    assert "--ignore-rules" in argv


def test_validate_codex_personality_config_overrides_requires_model_messages() -> None:
    for key in ("model_personality", "personality"):
        issue = validate_codex_personality_config_overrides(
            [
                f'{key}="pragmatic"',
                "model_reasoning_effort=high",
            ]
        )

        assert issue is not None
        assert "model_messages is missing" in issue.message
        assert "model_messages" in issue.hint
        assert issue.details.get("personality_keys") == [key]
        assert issue.details.get("model_messages_keys") == []


def test_validate_codex_personality_config_overrides_accepts_matching_model_messages() -> None:
    issue = validate_codex_personality_config_overrides(
        [
            'personality="pragmatic"',
            'model_messages=[{role="system", content="Be concise."}]',
        ]
    )

    assert issue is None


def test_validate_codex_personality_config_overrides_rejects_none_without_model_messages() -> None:
    issue = validate_codex_personality_config_overrides(
        [
            'personality="none"',
            "model_reasoning_effort=high",
        ]
    )

    assert issue is not None
    assert "model_messages is missing" in issue.message
    assert issue.details.get("personality_keys") == ["personality"]


def test_validate_codex_reasoning_effort_config_overrides_rejects_invalid_value() -> None:
    issue = validate_codex_reasoning_effort_config_overrides(
        [
            "model_reasoning_effort=xhigh",
            "profile.model_reasoning_effort='xhigh'",
        ]
    )

    assert issue is not None
    assert "invalid" in issue.message.lower()
    assert "xhigh" in issue.message
    assert "model_reasoning_effort=high" in issue.hint
    details = issue.details
    assert details.get("allowed_values") == ["minimal", "low", "medium", "high"]
    invalid_entries = details.get("invalid_entries")
    assert isinstance(invalid_entries, list)
    assert len(invalid_entries) == 2


def test_validate_codex_reasoning_effort_config_overrides_accepts_supported_value() -> None:
    issue = validate_codex_reasoning_effort_config_overrides(
        [
            "model_reasoning_effort=high",
        ]
    )

    assert issue is None
