from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

import runner_core.preflight as preflight_mod
from runner_core.preflight import _run_bounded_command_probe


def _process_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_timeout = 258
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)

    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            fields = proc_stat.read_text(encoding="utf-8").split()
        except OSError:
            return False
        if len(fields) >= 3 and fields[2] == "Z":
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _force_terminate_pid(pid: int) -> None:
    if not _process_is_running(pid):
        return
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_terminate = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_terminate, False, pid)
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    os.kill(pid, signal.SIGKILL)


def _wait_for_process_exit(pid: int, *, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _process_is_running(pid):
            return True
        time.sleep(0.025)
    return not _process_is_running(pid)


def _windows_job_process_ids(job_handle: int) -> tuple[int, list[int]]:
    import ctypes
    from ctypes import wintypes

    class _JobObjectBasicProcessIdList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 64),
        ]

    job_object_basic_process_id_list = 3
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    info = _JobObjectBasicProcessIdList()
    returned_length = wintypes.DWORD()
    queried = kernel32.QueryInformationJobObject(
        wintypes.HANDLE(job_handle),
        job_object_basic_process_id_list,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned_length),
    )
    if not queried:
        error = ctypes.get_last_error()
        raise OSError(error, "QueryInformationJobObject failed")
    return int(info.NumberOfAssignedProcesses), [
        int(info.ProcessIdList[index])
        for index in range(info.NumberOfProcessIdsInList)
    ]


def _write_descendant_probe_programs(tmp_path: Path) -> tuple[Path, Path]:
    descendant = tmp_path / "probe_descendant.py"
    descendant.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "import sys",
                "import time",
                "from pathlib import Path",
                "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')",
                "print('descendant-stdout-open', flush=True)",
                "print('descendant-stderr-open', file=sys.stderr, flush=True)",
                "time.sleep(120)",
                "",
            ]
        ),
        encoding="utf-8",
    )

    launcher = tmp_path / "probe_launcher.py"
    launcher.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                "print('launcher-stdout', flush=True)",
                "print('launcher-stderr', file=sys.stderr, flush=True)",
                "pid_path = Path(sys.argv[2])",
                "subprocess.Popen(",
                "    [sys.executable, sys.argv[1], str(pid_path)],",
                "    stdin=subprocess.DEVNULL,",
                "    stdout=sys.stdout,",
                "    stderr=sys.stderr,",
                ")",
                "deadline = time.monotonic() + 10.0",
                "while not pid_path.exists() and time.monotonic() < deadline:",
                "    time.sleep(0.01)",
                "if not pid_path.exists():",
                "    raise SystemExit(3)",
                "if len(sys.argv) > 3 and sys.argv[3] == 'wait':",
                "    time.sleep(120)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return launcher, descendant


def test_probe_returns_when_exited_launcher_leaves_captured_handles_open(tmp_path: Path) -> None:
    launcher, descendant = _write_descendant_probe_programs(tmp_path)
    descendant_pid_path = tmp_path / "descendant.pid"

    started = time.monotonic()
    result = _run_bounded_command_probe(
        [sys.executable, str(launcher), str(descendant), str(descendant_pid_path)],
        timeout_seconds=30.0,
    )
    elapsed = time.monotonic() - started
    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))

    try:
        assert elapsed < 45.0
        assert result.timed_out is False
        assert result.returncode == 0
        assert "launcher-stdout" in result.stdout
        assert "descendant-stdout-open" in result.stdout
        assert "launcher-stderr" in result.stderr
        assert "descendant-stderr-open" in result.stderr
        assert result.cleanup_succeeded is True, result.cleanup_diagnostic
        assert _wait_for_process_exit(descendant_pid)
    finally:
        _force_terminate_pid(descendant_pid)


def test_probe_timeout_returns_diagnostics_and_kills_descendant_tree(tmp_path: Path) -> None:
    launcher, descendant = _write_descendant_probe_programs(tmp_path)
    descendant_pid_path = tmp_path / "descendant.pid"

    started = time.monotonic()
    result = _run_bounded_command_probe(
        [
            sys.executable,
            str(launcher),
            str(descendant),
            str(descendant_pid_path),
            "wait",
        ],
        timeout_seconds=15.0,
    )
    elapsed = time.monotonic() - started
    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))

    try:
        assert elapsed < 30.0
        assert result.timed_out is True
        assert result.returncode == 124
        assert "launcher-stdout" in result.stdout
        assert "descendant-stdout-open" in result.stdout
        assert "launcher-stderr" in result.stderr
        assert "descendant-stderr-open" in result.stderr
        assert result.cleanup_succeeded is True, result.cleanup_diagnostic
        assert _wait_for_process_exit(descendant_pid)
    finally:
        _force_terminate_pid(descendant_pid)


@pytest.mark.parametrize(
    "command",
    [
        "pdm",
        pytest.param(
            "bash",
            marks=pytest.mark.skipif(
                os.name != "nt",
                reason="Windows bash usability probe contract",
            ),
        ),
    ],
)
def test_successful_probe_with_unverified_tree_cleanup_is_not_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    fake_executable = tmp_path / "tool.exe"
    monkeypatch.setattr(
        preflight_mod.shutil,
        "which",
        lambda _command, path=None: str(fake_executable),
    )
    monkeypatch.setattr(
        preflight_mod,
        "_run_bounded_command_probe",
        lambda *_args, **_kwargs: preflight_mod._BoundedCommandProbeResult(
            returncode=0,
            stdout="shell_probe=ok\n",
            stderr="",
            timed_out=False,
            cleanup_succeeded=False,
            cleanup_diagnostic="forced descendant cleanup failure",
        ),
    )

    availability, meta = preflight_mod._probe_commands_local([command])

    assert availability == {command: False}
    command_probe = meta["command_probe_details"][command]
    assert command_probe["usable"] is False
    assert command_probe["reason_code"] == "probe_cleanup_failed"
    assert "forced descendant cleanup failure" in command_probe["reason"]
    shell = meta["shell_probe"]
    assert shell["exit_code"] == 125
    assert shell["reason_code"] == "probe_cleanup_failed"
    assert shell["probe_tree_cleanup_succeeded"] is False
    assert "forced descendant cleanup failure" in shell["stderr"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment contract")
def test_windows_containment_failure_never_releases_target_or_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_marker = tmp_path / "target.started"
    descendant_marker = tmp_path / "descendant.started"
    descendant = tmp_path / "should_not_start_descendant.py"
    descendant.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    target = tmp_path / "should_not_start_target.py"
    target.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3]])\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        preflight_mod,
        "_attach_windows_probe_job",
        lambda _proc: (None, "forced Job Object attachment failure"),
    )

    started = time.monotonic()
    result = _run_bounded_command_probe(
        [
            sys.executable,
            str(target),
            str(target_marker),
            str(descendant),
            str(descendant_marker),
        ],
        timeout_seconds=30.0,
    )
    elapsed = time.monotonic() - started

    time.sleep(0.2)
    assert elapsed < 15.0
    assert result.returncode == 125
    assert result.timed_out is False
    assert result.cleanup_succeeded is True, result.cleanup_diagnostic
    assert "target was not launched" in result.stderr
    assert "forced Job Object attachment failure" in result.stderr
    assert not target_marker.exists()
    assert not descendant_marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-target contract")
def test_windows_containment_does_not_run_an_environment_injected_python_gate(
    tmp_path: Path,
) -> None:
    site_dir = tmp_path / "injected_site"
    site_dir.mkdir()
    spawned_pid_path = tmp_path / "precontainment_child.pid"
    sitecustomize = site_dir / "sitecustomize.py"
    sitecustomize.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        "    env={key: value for key, value in os.environ.items() if key != 'PYTHONPATH'},\n"
        ")\n"
        f"Path({str(spawned_pid_path)!r}).write_text(str(child.pid), encoding='utf-8')\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site_dir)

    result = _run_bounded_command_probe(
        ["cmd.exe", "/d", "/c", "exit", "0"],
        timeout_seconds=30.0,
        env=env,
    )

    escaped_pid = (
        int(spawned_pid_path.read_text(encoding="utf-8"))
        if spawned_pid_path.is_file()
        else None
    )
    try:
        assert result.returncode == 0
        assert result.timed_out is False
        assert result.cleanup_succeeded is True, result.cleanup_diagnostic
        assert escaped_pid is None, (
            "Windows containment executed user sitecustomize before Job Object assignment"
        )
    finally:
        if escaped_pid is not None:
            _force_terminate_pid(escaped_pid)


@pytest.mark.skipif(
    os.name != "nt"
    or os.path.normcase(os.path.abspath(sys.executable))
    == os.path.normcase(
        os.path.abspath(str(getattr(sys, "_base_executable", sys.executable)))
    ),
    reason="Windows virtual-environment Python redirector regression",
)
def test_windows_redirector_escape_is_tracked_outside_the_owned_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, descendant = _write_descendant_probe_programs(tmp_path)
    descendant_pid_path = tmp_path / "redirector-descendant.pid"
    observed_job_membership: list[tuple[int, list[int]]] = []
    original_terminate = preflight_mod._terminate_windows_job_processes

    def capture_job_membership(job_handle: int) -> tuple[bool, str | None]:
        observed_job_membership.append(_windows_job_process_ids(job_handle))
        return original_terminate(job_handle)

    monkeypatch.setattr(
        preflight_mod,
        "_terminate_windows_job_processes",
        capture_job_membership,
    )

    result = _run_bounded_command_probe(
        [
            sys.executable,
            str(launcher),
            str(descendant),
            str(descendant_pid_path),
            "wait",
        ],
        timeout_seconds=15.0,
    )
    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))

    try:
        assert result.returncode == 124
        assert result.timed_out is True
        assert result.cleanup_succeeded is True, result.cleanup_diagnostic
        assert observed_job_membership
        assigned_count, job_pids = observed_job_membership[0]
        assert assigned_count == 1
        assert len(job_pids) == 1
        assert descendant_pid not in job_pids
        assert _wait_for_process_exit(descendant_pid)
    finally:
        _force_terminate_pid(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object containment contract")
def test_windows_kill_on_close_cleans_tree_when_explicit_termination_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, descendant = _write_descendant_probe_programs(tmp_path)
    descendant_pid_path = tmp_path / "descendant.pid"
    monkeypatch.setattr(
        preflight_mod,
        "_terminate_windows_job_processes",
        lambda _job_handle: (False, "forced explicit termination failure"),
    )

    result = _run_bounded_command_probe(
        [sys.executable, str(launcher), str(descendant), str(descendant_pid_path)],
        timeout_seconds=30.0,
    )
    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))

    try:
        assert result.returncode == 0
        assert result.timed_out is False
        assert result.cleanup_succeeded is True, result.cleanup_diagnostic
        assert "forced explicit termination failure" in str(result.cleanup_diagnostic)
        assert "kill-on-job-close fallback" in str(result.cleanup_diagnostic)
        assert _wait_for_process_exit(descendant_pid)
    finally:
        _force_terminate_pid(descendant_pid)
