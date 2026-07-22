from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

_EXPECTED_COMMAND_PATH = "./scripts/offline_first_success.ps1"
_EXPECTED_WINDOWS_COMMAND = (
    "powershell -NoProfile -ExecutionPolicy Bypass -File " + _EXPECTED_COMMAND_PATH
)
_MANGLED_PATH_SYMPTOM = ".scriptsoffline_first_success.ps1"
_LIVE_POLICY_NAME = "write"
_SUCCESS_LINE_RE = re.compile(r"^==> Success\. Scratch run dir:\s*(.+?)\s*$", re.MULTILINE)
_WINDOWS_ABSOLUTE_SCRIPT_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/]|/[a-z]/)[^\r\n]*offline_first_success\.ps1"
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _read_readme_windows_command(repo_root: Path) -> str:
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    try:
        quickstart = readme.split("## Quickstart (one command)", 1)[1]
        quickstart = quickstart.split("## Developer smoke", 1)[0]
    except IndexError as exc:
        raise RuntimeError("README Windows quickstart section was not found") from exc

    match = re.search(
        r"- \*\*Windows PowerShell:\*\*\s*"
        r"```powershell\s*\r?\n(?P<command>[^\r\n]+)\r?\n\s*```",
        quickstart,
    )
    if match is None:
        raise RuntimeError("README Windows quickstart command was not found")
    return match.group("command").strip()


def _require_expected_command(command: str) -> None:
    if command != _EXPECTED_WINDOWS_COMMAND:
        raise RuntimeError(
            "README Windows quickstart command does not use the accepted cross-shell-safe spelling"
        )


def _git_bash_candidates() -> Iterable[Path]:
    configured = os.environ.get("GIT_BASH_EXE")
    if configured:
        yield Path(configured)

    git = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent
        yield git_root / "bin" / "bash.exe"
        yield git_root / "usr" / "bin" / "bash.exe"

    for env_name in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        yield Path(base) / "Git" / "bin" / "bash.exe"
        yield Path(base) / "Programs" / "Git" / "bin" / "bash.exe"

    path_bash = shutil.which("bash")
    if path_bash:
        candidate = Path(path_bash)
        if not (
            os.name == "nt"
            and candidate.resolve() == Path(os.environ.get("WINDIR", r"C:\Windows"))
            / "System32"
            / "bash.exe"
        ):
            yield candidate


def _find_git_bash() -> Path:
    checked: set[str] = set()
    for candidate in _git_bash_candidates():
        key = os.path.normcase(os.path.abspath(candidate))
        if key in checked:
            continue
        checked.add(key)
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "Git for Windows Bash was not found; set GIT_BASH_EXE to its bash.exe path"
    )


def _scratch_run_dirs() -> set[Path]:
    temp_root = Path(tempfile.gettempdir())
    runs: set[Path] = set()
    for report in temp_root.glob("usertest_fixture_*/*/report.md"):
        try:
            runs.add(report.parent.resolve())
        except OSError:
            continue
    return runs


def _scratch_run_from_output(output: str) -> Path | None:
    matches = list(_SUCCESS_LINE_RE.finditer(output))
    if not matches:
        return None
    raw_path = matches[-1].group(1).strip().strip('"')
    return Path(raw_path)


def _resolve_scratch_run(output: str, before: set[Path]) -> Path | None:
    from_output = _scratch_run_from_output(output)
    if from_output is not None and (from_output / "report.md").is_file():
        return from_output.resolve()

    new_runs = [path for path in _scratch_run_dirs() - before if (path / "report.md").is_file()]
    if not new_runs:
        return None
    return max(new_runs, key=lambda path: (path / "report.md").stat().st_mtime_ns)


def _run_readme_git_bash(repo_root: Path) -> dict[str, Any]:
    command = _read_readme_windows_command(repo_root)
    _require_expected_command(command)
    bash = _find_git_bash()
    before = _scratch_run_dirs()

    child = subprocess.run(
        [str(bash), "-lc", command],
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=1_800,
        check=False,
    )
    combined = f"{child.stdout}\n{child.stderr}"
    scratch_run = _resolve_scratch_run(combined, before)
    report_exists = bool(scratch_run and (scratch_run / "report.md").is_file())
    observation: dict[str, Any] = {
        "mode": "readme-git-bash",
        "readme_read": True,
        "command_path": _EXPECTED_COMMAND_PATH,
        "relative_script_attempt_count": command.count(_EXPECTED_COMMAND_PATH),
        "child_exit_code": child.returncode,
        "report_md_exists": report_exists,
        "absolute_path_retry_count": 0,
        "mangled_path_symptom": _MANGLED_PATH_SYMPTOM in combined,
        "git_bash_path": str(bash),
        "scratch_run_dir": str(scratch_run) if scratch_run else None,
    }
    observation["ok"] = all(
        (
            observation["relative_script_attempt_count"] == 1,
            observation["child_exit_code"] == 0,
            observation["report_md_exists"] is True,
            observation["absolute_path_retry_count"] == 0,
            observation["mangled_path_symptom"] is False,
        )
    )
    return observation


def _load_live_claude_config(repo_root: Path) -> dict[str, Any]:
    agents = yaml.safe_load((repo_root / "configs" / "agents.yaml").read_text(encoding="utf-8"))
    policies = yaml.safe_load(
        (repo_root / "configs" / "policies.yaml").read_text(encoding="utf-8")
    )
    try:
        agent = agents["agents"]["claude"]
        policy = policies["policies"][_LIVE_POLICY_NAME]["claude"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Claude agent or write-policy configuration is missing") from exc
    if not isinstance(agent, dict) or not isinstance(policy, dict):
        raise RuntimeError("Claude agent or write-policy configuration is invalid")
    return {"agent": agent, "policy": policy}


def _iter_raw_claude_messages(raw_events_path: Path) -> Iterable[dict[str, Any]]:
    with raw_events_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
                yield payload["message"]


def _tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _read_path_is_readme(path_value: Any, repo_root: Path) -> bool:
    if not isinstance(path_value, str) or not path_value.strip():
        return False
    candidate = Path(path_value.strip())
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        return candidate.resolve() == (repo_root / "README.md").resolve()
    except OSError:
        return False


def _inspect_live_claude_events(
    *, raw_events_path: Path, repo_root: Path, readme_command: str
) -> dict[str, Any]:
    tool_uses: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    for message in _iter_raw_claude_messages(raw_events_path):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_id = block.get("id")
                if isinstance(tool_id, str):
                    tool_uses[tool_id] = block
            elif block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id") or block.get("id")
                if isinstance(tool_id, str):
                    tool_results[tool_id] = block

    readme_read = False
    bash_attempts: list[dict[str, Any]] = []
    combined_tool_output: list[str] = []
    for tool_id, tool_use in tool_uses.items():
        name = str(tool_use.get("name", "")).strip().lower()
        tool_input = tool_use.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        result = tool_results.get(tool_id)
        succeeded = bool(result is not None and not result.get("is_error", False))
        if result is not None:
            combined_tool_output.append(_tool_result_text(result.get("content")))

        if name == "read":
            path_value = tool_input.get("file_path") or tool_input.get("path")
            if succeeded and _read_path_is_readme(path_value, repo_root):
                readme_read = True
        elif name == "bash":
            command = tool_input.get("command") or tool_input.get("cmd")
            if isinstance(command, str):
                bash_attempts.append(
                    {
                        "command": command.strip(),
                        "exit_code": 0 if succeeded else 1,
                        "output": _tool_result_text(result.get("content")) if result else "",
                    }
                )

    script_attempts = [
        attempt
        for attempt in bash_attempts
        if "offline_first_success.ps1" in attempt["command"].lower()
    ]
    relative_attempts = [
        attempt for attempt in script_attempts if _EXPECTED_COMMAND_PATH in attempt["command"]
    ]
    exact_attempts = [
        attempt for attempt in script_attempts if attempt["command"] == readme_command
    ]
    absolute_attempts = [
        attempt
        for attempt in script_attempts
        if _WINDOWS_ABSOLUTE_SCRIPT_RE.search(attempt["command"])
    ]
    child_exit_code = exact_attempts[0]["exit_code"] if len(exact_attempts) == 1 else None
    exact_output = exact_attempts[0]["output"] if len(exact_attempts) == 1 else ""
    return {
        "readme_read": readme_read,
        "relative_script_attempt_count": len(relative_attempts),
        "exact_script_attempt_count": len(exact_attempts),
        "absolute_path_retry_count": len(absolute_attempts),
        "unexpected_script_attempt_count": len(script_attempts) - len(exact_attempts),
        "child_exit_code": child_exit_code,
        "exact_command_output": exact_output,
        "combined_tool_output": "\n".join(combined_tool_output),
    }


def _git_status(repo_root: Path) -> bytes:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not capture repository status for the live Claude probe")
    return result.stdout


def _run_live_claude(repo_root: Path) -> dict[str, Any]:
    readme_command = _read_readme_windows_command(repo_root)
    _require_expected_command(readme_command)
    config = _load_live_claude_config(repo_root)
    agent = config["agent"]
    policy = config["policy"]
    allowed_tools = policy.get("allowed_tools")
    if (
        not isinstance(allowed_tools, list)
        or "Read" not in allowed_tools
        or "Bash" not in allowed_tools
    ):
        raise RuntimeError("Claude write policy must expose both Read and Bash")

    # Prefer this workspace's adapter source even when a shared virtualenv has another checkout
    # installed editable.
    adapter_src = repo_root / "packages" / "agent_adapters" / "src"
    sys.path.insert(0, str(adapter_src))
    from agent_adapters import run_claude_print

    artifacts_dir = Path(tempfile.mkdtemp(prefix="usertest_live_claude_probe_"))
    raw_events_path = artifacts_dir / "raw_events.jsonl"
    last_message_path = artifacts_dir / "agent_last_message.txt"
    stderr_path = artifacts_dir / "agent_stderr.txt"
    prompt = (
        "Perform this bounded Windows outcome check without modifying repository files.\n\n"
        "1. Use the Read tool to read README.md and locate its first "
        "'Quickstart (one command)' Windows PowerShell command.\n"
        "2. Use the Bash tool exactly once to execute that command verbatim from the "
        "current repository root.\n"
        "3. Do not run any other shell command, do not rewrite or quote the script path, "
        "and do not retry with an absolute path.\n"
        "4. Wait for the command to finish, then briefly state whether it succeeded. "
        "Do not use Edit, Agent, Grep, or Glob.\n\n"
        "The command must be copied from README.md; for validation, its expected script "
        f"token is {_EXPECTED_COMMAND_PATH}.\n"
    )

    status_before = _git_status(repo_root)
    scratch_before = _scratch_run_dirs()
    result = run_claude_print(
        workspace_dir=repo_root,
        prompt=prompt,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        binary=str(agent.get("binary", "claude")),
        output_format=str(agent.get("output_format", "stream-json")),
        model=str(agent["default_model"]) if agent.get("default_model") else None,
        allowed_tools=[str(item) for item in allowed_tools],
        permission_mode=(
            str(policy["permission_mode"]) if policy.get("permission_mode") else None
        ),
        max_turns=8,
    )
    status_after = _git_status(repo_root)
    inspected = _inspect_live_claude_events(
        raw_events_path=raw_events_path,
        repo_root=repo_root,
        readme_command=readme_command,
    )
    scratch_run = _resolve_scratch_run(inspected["exact_command_output"], scratch_before)
    report_exists = bool(scratch_run and (scratch_run / "report.md").is_file())
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    last_message = last_message_path.read_text(encoding="utf-8", errors="replace")
    observed_text = (
        f"{inspected['combined_tool_output']}\n{stderr_text}\n{last_message}"
    )

    observation: dict[str, Any] = {
        "mode": "live-claude",
        "policy": _LIVE_POLICY_NAME,
        "readme_read": inspected["readme_read"],
        "command_path": _EXPECTED_COMMAND_PATH,
        "relative_script_attempt_count": inspected["relative_script_attempt_count"],
        "child_exit_code": inspected["child_exit_code"],
        "report_md_exists": report_exists,
        "absolute_path_retry_count": inspected["absolute_path_retry_count"],
        "unexpected_script_attempt_count": inspected["unexpected_script_attempt_count"],
        "mangled_path_symptom": _MANGLED_PATH_SYMPTOM in observed_text,
        "claude_exit_code": result.exit_code,
        "repository_changed_by_probe": status_after != status_before,
        "scratch_run_dir": str(scratch_run) if scratch_run else None,
        "artifacts_dir": str(artifacts_dir),
    }
    observation["ok"] = all(
        (
            observation["claude_exit_code"] == 0,
            observation["readme_read"] is True,
            observation["relative_script_attempt_count"] == 1,
            inspected["exact_script_attempt_count"] == 1,
            observation["child_exit_code"] == 0,
            observation["report_md_exists"] is True,
            observation["absolute_path_retry_count"] == 0,
            observation["unexpected_script_attempt_count"] == 0,
            observation["mangled_path_symptom"] is False,
            observation["repository_changed_by_probe"] is False,
        )
    )
    return observation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the Windows offline-first quickstart.")
    parser.add_argument(
        "--mode",
        choices=("readme-git-bash", "live-claude"),
        required=True,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if os.name != "nt":
            raise RuntimeError("The Windows offline-first outcome probe requires Windows")
        repo_root = _repo_root()
        if args.mode == "readme-git-bash":
            observation = _run_readme_git_bash(repo_root)
        else:
            observation = _run_live_claude(repo_root)
    except Exception as exc:
        observation = {
            "mode": args.mode,
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    print(json.dumps(observation, sort_keys=True))
    return 0 if observation.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
