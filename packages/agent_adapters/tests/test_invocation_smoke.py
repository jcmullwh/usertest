from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from agent_adapters import run_claude_print, run_codex_exec, run_gemini


def _make_dummy_executable(tmp_path: Path) -> str:
    if os.name == "nt":
        path = tmp_path / "dummy_agent.cmd"
        path.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        return str(path)

    path = tmp_path / "dummy_agent.sh"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _make_prefix_shim(tmp_path: Path) -> str:
    if os.name == "nt":
        path = tmp_path / "prefix_shim.cmd"
        path.write_text("@echo off\r\n%*\r\nexit /b %ERRORLEVEL%\r\n", encoding="utf-8")
        return str(path)

    path = tmp_path / "prefix_shim.sh"
    path.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_claude_cli_includes_verbose_for_stream_json(tmp_path: Path) -> None:
    dummy = _make_dummy_executable(tmp_path)
    result = run_claude_print(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        output_format="stream-json",
    )
    assert result.exit_code == 0
    assert "--verbose" in result.argv


def test_claude_cli_includes_system_prompt_files(tmp_path: Path) -> None:
    dummy = _make_dummy_executable(tmp_path)
    system_path = tmp_path / "system.md"
    system_path.write_text("system\n", encoding="utf-8")
    append_path = tmp_path / "append.md"
    append_path.write_text("append\n", encoding="utf-8")

    result = run_claude_print(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        output_format="stream-json",
        system_prompt_file=system_path,
        append_system_prompt_file=append_path,
    )
    assert result.exit_code == 0
    pairs = set(zip(result.argv, result.argv[1:], strict=False))
    assert ("--system-prompt-file", str(system_path)) in pairs
    assert ("--append-system-prompt-file", str(append_path)) in pairs


def test_gemini_cli_injects_env_for_docker_exec_prefix(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker_bin"
    docker_dir.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        docker_shim = docker_dir / "docker_shim.py"
        docker_shim.write_text(
            "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "import subprocess",
                    "import sys",
                    "",
                    "",
                    "def main() -> int:",
                    "    args = sys.argv[1:]",
                    "    if not args or args[0] != 'exec':",
                    "        return 1",
                    "    idx = 1",
                    "    while idx < len(args):",
                    "        flag = args[idx]",
                    "        if flag in {'-i', '-t'}:",
                    "            idx += 1",
                    "            continue",
                    "        if flag in {'-w', '--workdir', '-e', '--env'}:",
                    "            idx += 2",
                    "            continue",
                    "        if flag.startswith('-'):",
                    "            idx += 1",
                    "            continue",
                    "        break",
                    "    if idx >= len(args):",
                    "        return 1",
                    "    # Skip container name.",
                    "    idx += 1",
                    "    if idx >= len(args):",
                    "        return 1",
                    "    proc = subprocess.run(args[idx:], check=False)",
                    "    return proc.returncode",
                    "",
                    "",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(main())",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        docker = docker_dir / "docker.cmd"
        docker.write_text(
            "\n".join(
                [
                    "@echo off",
                    f"\"{sys.executable}\" \"{docker_shim}\" %*",
                    "exit /b %ERRORLEVEL%",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        docker = docker_dir / "docker"
        docker.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    'if [ "$1" != "exec" ]; then exit 1; fi',
                    "shift",
                    "while [ $# -gt 0 ]; do",
                    '  case "$1" in',
                    "    -i) shift ;;",
                    "    -w) shift; shift ;;",
                    "    -e) shift; shift ;;",
                    "    -*) shift ;;",
                    "    *) break ;;",
                    "  esac",
                    "done",
                    "# container name",
                    "shift",
                    'exec "$@"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)

    dummy = _make_dummy_executable(tmp_path)
    result = run_gemini(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        sandbox=True,
        system_prompt_file="/run_dir/system.md",
        command_prefix=[str(docker), "exec", "-i", "-w", "/workspace", "container"],
        env_overrides={"SENTINEL": "1"},
    )
    assert result.exit_code == 0
    pairs = set(zip(result.argv, result.argv[1:], strict=False))
    assert ("-e", "SENTINEL=1") in pairs
    assert ("--agent-system-prompt-file", "/run_dir/system.md") in pairs


def test_gemini_cli_includes_sandbox_and_allowed_tools(tmp_path: Path) -> None:
    dummy = _make_dummy_executable(tmp_path)
    result = run_gemini(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        sandbox=True,
        allowed_tools=["read_file", "run_shell_command"],
    )
    assert result.exit_code == 0
    assert "--sandbox" in result.argv
    pairs = set(zip(result.argv, result.argv[1:], strict=False))
    assert ("--allowed-tools", "read_file") in pairs
    assert ("--allowed-tools", "run_shell_command") in pairs


def test_gemini_cli_includes_include_directories(tmp_path: Path) -> None:
    dummy = _make_dummy_executable(tmp_path)
    include_dir = str(Path("runs") / "usertest")
    result = run_gemini(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        sandbox=True,
        include_directories=[include_dir],
    )
    assert result.exit_code == 0
    pairs = set(zip(result.argv, result.argv[1:], strict=False))
    assert ("--include-directories", include_dir) in pairs


def test_codex_cli_includes_sandbox_and_stdin_prompt(tmp_path: Path) -> None:
    dummy = _make_dummy_executable(tmp_path)
    result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        sandbox="read-only",
    )
    assert result.exit_code == 0
    assert "--sandbox" in result.argv
    assert "read-only" in result.argv
    assert result.argv[-1] == "-"


def test_invocations_prefix_with_command_prefix(tmp_path: Path) -> None:
    dummy = _make_dummy_executable(tmp_path)
    prefix = _make_prefix_shim(tmp_path)

    claude_result = run_claude_print(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw.jsonl",
        last_message_path=tmp_path / "last.txt",
        stderr_path=tmp_path / "stderr.txt",
        binary=dummy,
        output_format="stream-json",
        command_prefix=[prefix],
    )
    assert claude_result.exit_code == 0
    assert claude_result.argv[:2] == [prefix, dummy]

    gemini_result = run_gemini(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw2.jsonl",
        last_message_path=tmp_path / "last2.txt",
        stderr_path=tmp_path / "stderr2.txt",
        binary=dummy,
        sandbox=True,
        command_prefix=[prefix],
    )
    assert gemini_result.exit_code == 0
    assert gemini_result.argv[:2] == [prefix, dummy]

    codex_result = run_codex_exec(
        workspace_dir=tmp_path,
        prompt="hi",
        raw_events_path=tmp_path / "raw3.jsonl",
        last_message_path=tmp_path / "last3.txt",
        stderr_path=tmp_path / "stderr3.txt",
        binary=dummy,
        sandbox="read-only",
        command_prefix=[prefix],
    )
    assert codex_result.exit_code == 0
    assert codex_result.argv[:2] == [prefix, dummy]
