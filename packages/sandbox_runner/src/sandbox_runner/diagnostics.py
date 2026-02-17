from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any


def probe_commands_in_container(
    *, command_prefix: list[str], commands: list[str]
) -> tuple[dict[str, bool], dict[str, Any]]:
    safe_cmds = [c for c in commands if isinstance(c, str) and c.strip()]
    if not safe_cmds:
        return {}, {}

    shell_list = " ".join(shlex.quote(c) for c in safe_cmds)
    script = (
        "set +e\n"
        "for c in "
        + shell_list
        + " ; do\n"
        "  if command -v \"$c\" >/dev/null 2>&1; then echo \"$c=1\"; else echo \"$c=0\"; fi\n"
        "done\n"
        "uid=$(id -u 2>/dev/null)\n"
        "if [ -n \"$uid\" ]; then echo \"uid=$uid\"; fi\n"
    )

    try:
        proc = subprocess.run(
            [*command_prefix, "sh", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return {}, {"error": str(e)}

    present: dict[str, bool] = {}
    uid: int | None = None
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", maxsplit=1)
        key = key.strip()
        val = val.strip()
        if key == "uid":
            try:
                uid = int(val)
            except ValueError:
                uid = None
            continue
        if key in safe_cmds:
            present[key] = val == "1"

    meta: dict[str, Any] = {
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip(),
        "uid": uid,
    }
    return present, meta


def capture_dns_snapshot(*, command_prefix: list[str], artifacts_dir: Path) -> None:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out_path = artifacts_dir / "dns_snapshot.txt"

    script = "\n".join(
        [
            "set +e",
            'echo "## /etc/resolv.conf"',
            "cat /etc/resolv.conf 2>&1",
            "echo",
            'echo "## /etc/hosts"',
            "cat /etc/hosts 2>&1",
            "echo",
            'echo "## getent hosts www.python.org"',
            "getent hosts www.python.org 2>&1",
            "echo",
            'echo "## getent hosts pypi.org"',
            "getent hosts pypi.org 2>&1",
            "exit 0",
        ]
    )

    try:
        proc = subprocess.run(
            [*command_prefix, "sh", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        out_path.write_text(
            "\n".join(
                [
                    "$ docker exec ... sh -lc '<dns snapshot script>'",
                    f"exit_code={proc.returncode}",
                    "",
                    "stdout:",
                    proc.stdout.strip(),
                    "",
                    "stderr:",
                    proc.stderr.strip(),
                    "",
                ]
            ),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        try:
            out_path.write_text(f"Failed to capture dns snapshot: {e}\n", encoding="utf-8")
        except Exception:
            return


def capture_container_artifacts(*, container_name: str, artifacts_dir: Path) -> None:
    if not isinstance(container_name, str) or not container_name.strip():
        return

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logs_path = artifacts_dir / "container_logs.txt"
    inspect_path = artifacts_dir / "container_inspect.json"

    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _load_env_allowlist_for_scrub() -> set[str]:
        sandbox_meta_path = artifacts_dir / "sandbox.json"
        try:
            payload = json.loads(sandbox_meta_path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if not isinstance(payload, dict):
            return set()
        raw = payload.get("env_allowlist")
        if not isinstance(raw, list):
            return set()
        return {str(x).strip() for x in raw if isinstance(x, str) and x.strip()}

    def _scrub_docker_inspect(text: str, *, env_allowlist: set[str]) -> str:
        if not env_allowlist or not isinstance(text, str) or not text.strip():
            return text
        try:
            payload = json.loads(text)
        except Exception:
            return text
        if not isinstance(payload, list):
            return text

        changed = False
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            config = entry.get("Config")
            if not isinstance(config, dict):
                continue
            env = config.get("Env")
            if not isinstance(env, list):
                continue
            scrubbed: list[object] = []
            for item in env:
                if not isinstance(item, str) or "=" not in item:
                    scrubbed.append(item)
                    continue
                key, _value = item.split("=", maxsplit=1)
                if key in env_allowlist:
                    scrubbed.append(f"{key}=<redacted>")
                    changed = True
                else:
                    scrubbed.append(item)
            config["Env"] = scrubbed

        if not changed:
            return text
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    env_allowlist = _load_env_allowlist_for_scrub()

    try:
        proc = subprocess.run(
            ["docker", "logs", container_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        logs_path.write_text(
            "\n".join(
                [
                    f"$ docker logs {container_name}",
                    f"exit_code={proc.returncode}",
                    "",
                    "stdout:",
                    proc.stdout.strip(),
                    "",
                    "stderr:",
                    proc.stderr.strip(),
                    "",
                ]
            ),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        try:
            logs_path.write_text(f"Failed to capture docker logs: {e}\n", encoding="utf-8")
        except Exception:
            return

    try:
        proc = subprocess.run(
            ["docker", "inspect", container_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            inspect_path.write_text(
                _scrub_docker_inspect(proc.stdout, env_allowlist=env_allowlist).rstrip() + "\n",
                encoding="utf-8",
            )
        else:
            _write_json(
                inspect_path,
                {
                    "error": "docker inspect failed",
                    "container_name": container_name,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout.strip(),
                    "stderr": proc.stderr.strip(),
                },
            )
    except Exception as e:  # noqa: BLE001
        try:
            _write_json(
                inspect_path,
                {
                    "error": "docker inspect failed",
                    "container_name": container_name,
                    "message": str(e),
                },
            )
        except Exception:
            return
