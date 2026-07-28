from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_adapters.codex_cli import (
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    CodexLoginStatusResult,
    _resolve_executable,
    _resolve_windows_desktop_codex_executable,
    _windows_desktop_codex_candidates,
    build_codex_subscription_config_overrides,
    codex_subscription_config_errors,
    probe_codex_login_status,
    resolve_codex_executable,
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


def _make_completed_turn_hanging_dummy_codex(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_completed_turn_hang.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "import time",
                "from pathlib import Path",
                "",
                "sys.stdin.read()",
                "output_index = sys.argv.index('--output-last-message') + 1",
                "Path(sys.argv[output_index]).write_text(",
                "    '{\"status\":\"partial\"}', encoding='utf-8'",
                ")",
                "print(json.dumps({'type': 'thread.started', 'thread_id': "
                "'019f2cca-9011-7e32-88ae-6c25af578b49'}), flush=True)",
                "print(json.dumps({'type': 'agent_message', 'text': "
                "'{\"status\":\"partial\"}'}), flush=True)",
                "print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)",
                "time.sleep(15)",
                "raise SystemExit(7)",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    if os.name == "nt":
        wrapper = tmp_path / "dummy_codex_completed_turn_hang.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="\n",
        )
        return str(wrapper)
    wrapper = tmp_path / "dummy_codex_completed_turn_hang.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
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


def test_windows_desktop_codex_candidates_are_discovered_by_structure_and_recency(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "OpenAI" / "Codex" / "bin"
    older = bin_dir / "opaque-old-install" / "codex.exe"
    newer = bin_dir / "opaque-current-install" / "codex.exe"
    unrelated = bin_dir / "not-a-codex-install" / "helper.exe"
    for path in (older, newer, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    candidates = _windows_desktop_codex_candidates(local_app_data=str(tmp_path))

    assert candidates == [newer, older]


def test_windows_desktop_codex_resolution_skips_unusable_newer_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "OpenAI" / "Codex" / "bin"
    older_usable = bin_dir / "older" / "codex.exe"
    newer_unusable = bin_dir / "newer" / "codex.exe"
    for path in (older_usable, newer_unusable):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    os.utime(older_usable, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer_unusable, ns=(2_000_000_000, 2_000_000_000))
    observed: list[Path] = []

    def _usable(candidate: Path) -> bool:
        observed.append(candidate)
        return candidate == older_usable

    monkeypatch.setattr("agent_adapters.codex_cli._codex_executable_is_usable", _usable)

    resolved = _resolve_windows_desktop_codex_executable(local_app_data=str(tmp_path))

    assert resolved == str(older_usable)
    assert observed == [newer_unusable, older_usable]


def test_resolve_codex_executable_honors_explicit_configured_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "pinned" / "codex.exe"
    monkeypatch.setattr(
        "agent_adapters.codex_cli._resolve_windows_desktop_codex_executable",
        lambda **_: pytest.fail("Desktop discovery must not replace an explicit path"),
    )

    assert resolve_codex_executable(str(explicit)) == str(explicit)


@pytest.mark.skipif(os.name != "nt", reason="Windows Codex Desktop binary preference")
def test_resolve_plain_codex_prefers_desktop_binary_over_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = tmp_path / "desktop" / "codex.exe"
    path_binary = tmp_path / "npm" / "codex.cmd"
    monkeypatch.setattr(
        "agent_adapters.codex_cli._resolve_windows_desktop_codex_executable",
        lambda **_: str(desktop),
    )
    monkeypatch.setattr(
        "agent_adapters.codex_cli.shutil.which",
        lambda *_args, **_kwargs: str(path_binary),
    )

    assert resolve_codex_executable("codex") == str(desktop)


@pytest.mark.skipif(os.name != "nt", reason="Windows Codex Desktop binary fallback")
def test_resolve_plain_codex_falls_back_to_path_when_desktop_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_binary = tmp_path / "npm" / "codex.cmd"
    monkeypatch.setattr(
        "agent_adapters.codex_cli._resolve_windows_desktop_codex_executable",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "agent_adapters.codex_cli.shutil.which",
        lambda *_args, **_kwargs: str(path_binary),
    )

    assert resolve_codex_executable("codex") == str(path_binary)


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


@pytest.mark.skipif(os.name != "nt", reason="Windows Codex Desktop login resolution")
def test_probe_codex_login_status_uses_desktop_resolver_without_changing_auth_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_binary = _make_login_status_dummy_codex(tmp_path)
    capture = tmp_path / "desktop-status-capture.json"
    host_codex_home = tmp_path / "host-codex-home"
    host_codex_home.mkdir()
    monkeypatch.setattr(
        "agent_adapters.codex_cli._resolve_windows_desktop_codex_executable",
        lambda **_: desktop_binary,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")

    result = probe_codex_login_status(
        binary="codex",
        codex_home=host_codex_home,
        cwd=tmp_path,
        env_overrides={"CODEX_STATUS_CAPTURE": str(capture)},
    )

    assert result.ok is True
    assert Path(result.argv[0]).resolve() == Path(desktop_binary).resolve()
    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert Path(observed["CODEX_HOME"]).resolve() == host_codex_home.resolve()
    assert observed["OPENAI_API_KEY"] == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows Codex Desktop execution resolution")
def test_run_codex_exec_uses_desktop_resolver_for_plain_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_binary = _make_argv_dump_dummy_codex(tmp_path)
    argv_path = tmp_path / "desktop-exec-argv.json"
    monkeypatch.setattr(
        "agent_adapters.codex_cli._resolve_windows_desktop_codex_executable",
        lambda **_: desktop_binary,
    )

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="exercise local resolution only",
        raw_events_path=tmp_path / "desktop-exec-events.jsonl",
        last_message_path=tmp_path / "desktop-exec-last.txt",
        stderr_path=tmp_path / "desktop-exec-stderr.txt",
        sandbox="read-only",
        ask_for_approval="never",
        binary="codex",
        env_overrides={"CODEX_ARGV_OUT": str(argv_path)},
    )

    assert result.exit_code == 0
    assert Path(result.argv[0]).resolve() == Path(desktop_binary).resolve()
    assert json.loads(argv_path.read_text(encoding="utf-8"))[0:2] == [
        "--ask-for-approval",
        "never",
    ]


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


def test_codex_login_status_accepts_chatgpt_after_known_desktop_launcher_warnings() -> None:
    result = CodexLoginStatusResult(
        argv=["codex", "login", "status"],
        exit_code=0,
        stdout="",
        stderr=(
            "WARNING: failed to clean up stale arg0 temp dirs: Access is denied. "
            "(os error 5)\n"
            "WARNING: proceeding, even though we could not create PATH aliases: "
            "Access is denied. (os error 5) at path "
            '"C:\\\\Users\\\\user\\\\.codex\\\\tmp\\\\arg0\\\\codex-arg0abc"\n'
            "Logged in using ChatGPT\n"
        ),
        codex_home=r"C:\Users\user\.codex",
        auth_env_vars_blank={name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    )

    assert result.ok is True
    redacted = result.to_redacted_dict()
    assert redacted["status_kind"] == "chatgpt"
    assert redacted["chatgpt_status_exact"] is True
    assert redacted["ignored_launcher_diagnostic_count"] == 2
    assert redacted["ignored_launcher_diagnostic_kinds"] == [
        "path_alias_creation_access_denied",
        "stale_arg0_cleanup_access_denied",
    ]


@pytest.mark.parametrize(
    ("extra_line", "expected_kind"),
    [
        ("WARNING: authentication cache changed unexpectedly", "unexpected"),
        ("Logged in using an API key", "api_key"),
    ],
)
def test_codex_login_status_does_not_ignore_unknown_or_contradictory_output(
    extra_line: str,
    expected_kind: str,
) -> None:
    result = CodexLoginStatusResult(
        argv=["codex", "login", "status"],
        exit_code=0,
        stdout="",
        stderr=(
            "WARNING: failed to clean up stale arg0 temp dirs: Access is denied. "
            "(os error 5)\n"
            f"{extra_line}\n"
            "Logged in using ChatGPT\n"
        ),
        codex_home="C:/host/.codex",
        auth_env_vars_blank={name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    )

    assert result.ok is False
    assert result.to_redacted_dict()["status_kind"] == expected_kind


@pytest.mark.parametrize(
    "tampered_warning",
    [
        (
            "WARNING: failed to clean up stale arg0 temp dirs: Access is denied. "
            "(os error 5) Logged in using an API key"
        ),
        (
            "WARNING: proceeding, even though we could not create PATH aliases: "
            "Access is denied. (os error 5) at path "
            '"C:\\\\outside\\\\codex-arg0abc"'
        ),
    ],
)
def test_codex_login_status_rejects_tampered_known_launcher_warning(
    tampered_warning: str,
) -> None:
    result = CodexLoginStatusResult(
        argv=["codex", "login", "status"],
        exit_code=0,
        stdout="",
        stderr=f"{tampered_warning}\nLogged in using ChatGPT\n",
        codex_home=r"C:\Users\user\.codex",
        auth_env_vars_blank={name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    )

    assert result.ok is False
    assert result.to_redacted_dict()["status_kind"] == "unexpected"


def test_codex_login_status_does_not_ignore_launcher_warning_on_stdout() -> None:
    result = CodexLoginStatusResult(
        argv=["codex", "login", "status"],
        exit_code=0,
        stdout=(
            "WARNING: failed to clean up stale arg0 temp dirs: Access is denied. "
            "(os error 5)\n"
        ),
        stderr="Logged in using ChatGPT\n",
        codex_home=r"C:\Users\user\.codex",
        auth_env_vars_blank={name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    )

    assert result.ok is False
    assert result.to_redacted_dict()["status_kind"] == "unexpected"


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


def test_run_codex_exec_salvages_persisted_terminal_turn_from_orphaned_process(
    tmp_path: Path,
) -> None:
    dummy_binary = _make_completed_turn_hanging_dummy_codex(tmp_path)
    stderr_path = tmp_path / "stderr.txt"
    raw_events_path = tmp_path / "raw_events.jsonl"
    last_message_path = tmp_path / "last_message.txt"

    started = time.monotonic()
    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="complete normally, then leave an orphan helper",
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        sandbox="read-only",
        ask_for_approval="never",
        binary=dummy_binary,
        agent_last_message_path="",
    )

    assert time.monotonic() - started < 10.0
    assert result.exit_code == 0
    assert result.terminal_turn_salvaged is True
    assert result.thread_id == "019f2cca-9011-7e32-88ae-6c25af578b49"
    assert json.loads(last_message_path.read_text(encoding="utf-8")) == {
        "status": "partial"
    }
    assert '"type": "turn.completed"' in raw_events_path.read_text(encoding="utf-8")
    assert "retained the completed turn" in stderr_path.read_text(encoding="utf-8")


def test_run_codex_exec_preserves_agent_visible_last_message_path(
    tmp_path: Path,
) -> None:
    dummy_binary = _make_argv_dump_dummy_codex(tmp_path)
    argv_path = tmp_path / "argv.json"
    agent_last_message_path = "/run_dir/attempts/attempt_001/agent_last_message.txt"

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="test",
        raw_events_path=tmp_path / "raw_events.jsonl",
        last_message_path=tmp_path / "last_message.txt",
        stderr_path=tmp_path / "stderr.txt",
        sandbox="read-only",
        ask_for_approval="never",
        binary=dummy_binary,
        agent_last_message_path=agent_last_message_path,
        env_overrides={"CODEX_ARGV_OUT": str(argv_path)},
    )

    assert result.exit_code == 0
    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    output_last_message_arg = argv[argv.index("--output-last-message") + 1]
    print(json.dumps({"output_last_message_arg": output_last_message_arg}))
    assert output_last_message_arg == agent_last_message_path


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


def test_run_codex_exec_resumes_exact_session_with_effective_workspace_and_sandbox(
    tmp_path: Path,
) -> None:
    dummy_binary = _make_argv_dump_dummy_codex(tmp_path)
    argv_path = tmp_path / "resume-argv.json"
    thread_id = "019f2cca-9011-7e32-88ae-6c25af578b49"

    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="correct the retained dossier",
        raw_events_path=tmp_path / "resume-events.jsonl",
        last_message_path=tmp_path / "resume-last.txt",
        stderr_path=tmp_path / "resume-stderr.txt",
        sandbox="workspace-write",
        ask_for_approval="never",
        binary=dummy_binary,
        ignore_rules=True,
        env_overrides={"CODEX_ARGV_OUT": str(argv_path)},
        resume_session_id=thread_id,
    )

    assert result.exit_code == 0
    assert result.thread_id == thread_id
    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    exec_index = argv.index("exec")
    cd_index = argv.index("--cd")
    sandbox_index = argv.index("--sandbox")
    assert argv[exec_index + 1] == "resume"
    assert cd_index < exec_index
    assert argv[cd_index + 1] == str(tmp_path)
    assert sandbox_index < exec_index
    assert argv[sandbox_index + 1] == "workspace-write"
    assert argv.count("--cd") == 1
    assert argv.count("--sandbox") == 1
    assert thread_id in argv
    assert argv[-1] == "-"
    assert "--last" not in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv


def test_run_codex_exec_rejects_noncanonical_resume_session(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="codex_resume_session_id_invalid"):
        run_codex_exec(
            workspace_dir=tmp_path,
            prompt="do not start a fresh session",
            raw_events_path=tmp_path / "events.jsonl",
            last_message_path=tmp_path / "last.txt",
            stderr_path=tmp_path / "stderr.txt",
            sandbox="read-only",
            binary="codex",
            resume_session_id="last",
        )


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
