from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_core.python_interpreter_probe import resolve_usable_python_interpreter

# These values bound small preflight probes only. They are deliberately named
# probe budgets rather than implementation-run timeouts so they cannot be
# mistaken for a cap on agent or verification execution.
_PYTHON_INTERPRETER_PROBE_BUDGET_SECONDS = 5.0
_PDM_VERSION_PROBE_BUDGET_SECONDS = 2.5
_BASH_LAUNCH_PROBE_BUDGET_SECONDS = 2.0
_LOCAL_SHELL_PAYLOAD_PROBE_BUDGET_SECONDS = 2.5
_PROBE_TREE_CLEANUP_BUDGET_SECONDS = 2.0
_PROBE_OUTPUT_CAPTURE_LIMIT_BYTES = 64 * 1024
_PROBE_CONTAINMENT_FAILURE_EXIT_CODE = 125
_WINDOWS_CREATE_SUSPENDED = 0x00000004

_BASE_PREFLIGHT_COMMANDS = [
    "git",
    "rg",
    "bash",
    "python3",
    "python",
    "py",
    "pip",
    "pip3",
    "pdm",
    "node",
    "npm",
    # Common package managers / installers (useful for dependency bootstrapping).
    "apt-get",
    "apk",
    "dnf",
    "yum",
    "pacman",
    "brew",
    "choco",
    "winget",
    "scoop",
]

def _build_preflight_command_list(request: Any) -> list[str]:
    """
    Build the ordered list of command names to probe during preflight.

    Preflight is intended to be generic: the baseline list contains common developer tooling and
    installer entry points, while repo-specific dependencies can be supplied per run via
    `RunRequest.preflight_commands` (CLI: `--preflight-command`) and required checks can be
    supplied via `RunRequest.preflight_required_commands` (CLI: `--require-preflight-command`).
    """

    merged: list[str] = []
    seen: set[str] = set()

    candidates: list[str] = [
        *_BASE_PREFLIGHT_COMMANDS,
        *request.preflight_commands,
        *request.preflight_required_commands,
    ]
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        cmd = raw.strip()
        if not cmd or cmd in seen:
            continue
        merged.append(cmd)
        seen.add(cmd)

    return merged

def _agent_binary_for_preflight_probe(*, agent: str, agent_cfg: dict[str, Any]) -> str | None:
    default_binary = {
        "codex": "codex",
        "claude": "claude",
        "gemini": "gemini",
    }.get(agent, "")
    raw_binary = agent_cfg.get("binary", default_binary)
    if not isinstance(raw_binary, str) or not raw_binary.strip():
        return None

    binary = raw_binary.strip()
    if Path(binary).is_absolute():
        return None
    if any(sep in binary for sep in ("/", "\\")):
        return None
    if os.name == "nt" and ":" in binary:
        return None

    return binary


@dataclass(frozen=True)
class _BoundedCommandProbeResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    cleanup_succeeded: bool
    cleanup_diagnostic: str | None


@dataclass(frozen=True)
class _WindowsTrackedProcess:
    pid: int
    parent_pid: int
    depth: int
    handle: int


class _WindowsProcessTreeTracker:
    """Retain handles for every observable descendant of a Windows probe root."""

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self._depth_by_pid: dict[int, int] = {root_pid: 0}
        self._tracked: dict[int, _WindowsTrackedProcess] = {}
        self._active_unverified: dict[int, str] = {}
        self._closed = False

    @staticmethod
    def _snapshot_process_parents() -> tuple[list[tuple[int, int]], str | None]:
        import ctypes
        from ctypes import wintypes

        class _ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        th32cs_snapprocess = 0x00000002
        invalid_handle_value = ctypes.c_void_p(-1).value
        error_no_more_files = 18

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        raw_snapshot_handle = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if not raw_snapshot_handle or int(raw_snapshot_handle) == invalid_handle_value:
            error = ctypes.get_last_error()
            return [], f"Process snapshot failed with Windows error {error}."
        snapshot_handle = int(raw_snapshot_handle)

        entries: list[tuple[int, int]] = []
        diagnostic: str | None = None
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            has_entry = kernel32.Process32FirstW(
                wintypes.HANDLE(snapshot_handle),
                ctypes.byref(entry),
            )
            if not has_entry:
                error = ctypes.get_last_error()
                if error != error_no_more_files:
                    diagnostic = (
                        f"Process32FirstW failed with Windows error {error}."
                    )
            while has_entry:
                entries.append(
                    (int(entry.th32ProcessID), int(entry.th32ParentProcessID))
                )
                has_entry = kernel32.Process32NextW(
                    wintypes.HANDLE(snapshot_handle),
                    ctypes.byref(entry),
                )
            if diagnostic is None:
                error = ctypes.get_last_error()
                if error not in {0, error_no_more_files}:
                    diagnostic = (
                        f"Process32NextW failed with Windows error {error}."
                    )
        finally:
            if not kernel32.CloseHandle(wintypes.HANDLE(snapshot_handle)):
                error = ctypes.get_last_error()
                close_diagnostic = (
                    f"CloseHandle(process snapshot) failed with Windows error {error}."
                )
                diagnostic = " ".join(
                    part for part in (diagnostic, close_diagnostic) if part
                )

        return entries, diagnostic

    @staticmethod
    def _open_process(pid: int) -> tuple[int | None, str | None]:
        import ctypes
        from ctypes import wintypes

        process_terminate = 0x0001
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        raw_handle = kernel32.OpenProcess(
            process_terminate | synchronize,
            False,
            pid,
        )
        if not raw_handle:
            error = ctypes.get_last_error()
            return None, f"OpenProcess({pid}) failed with Windows error {error}."
        return int(raw_handle), None

    def discover(self) -> tuple[bool, str | None]:
        if self._closed:
            return False, "Windows process-tree tracking was already closed."

        entries, snapshot_diagnostic = self._snapshot_process_parents()
        if snapshot_diagnostic is not None:
            return False, snapshot_diagnostic

        children_by_parent: dict[int, list[int]] = {}
        for pid, parent_pid in entries:
            if pid <= 0 or pid == parent_pid:
                continue
            children_by_parent.setdefault(parent_pid, []).append(pid)

        reachable: set[int] = set(self._depth_by_pid)
        active_unverified: dict[int, str] = {}
        frontier = list(self._depth_by_pid.items())
        while frontier:
            parent_pid, parent_depth = frontier.pop()
            for child_pid in children_by_parent.get(parent_pid, []):
                if child_pid in reachable:
                    continue
                reachable.add(child_pid)
                child_depth = parent_depth + 1
                tracked = self._tracked.get(child_pid)
                if tracked is None:
                    handle, open_diagnostic = self._open_process(child_pid)
                    if handle is None:
                        active_unverified[child_pid] = open_diagnostic or (
                            f"OpenProcess({child_pid}) failed."
                        )
                    else:
                        tracked = _WindowsTrackedProcess(
                            pid=child_pid,
                            parent_pid=parent_pid,
                            depth=child_depth,
                            handle=handle,
                        )
                        self._tracked[child_pid] = tracked
                        self._depth_by_pid[child_pid] = child_depth
                frontier.append((child_pid, child_depth))

        self._active_unverified = active_unverified
        return True, None

    @staticmethod
    def _handle_is_running(handle: int) -> tuple[bool | None, str | None]:
        import ctypes
        from ctypes import wintypes

        wait_object_0 = 0
        wait_timeout = 258
        wait_failed = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0)
        if result == wait_object_0:
            return False, None
        if result == wait_timeout:
            return True, None
        error = ctypes.get_last_error() if result == wait_failed else result
        return None, f"WaitForSingleObject failed with Windows result {error}."

    @staticmethod
    def _terminate_handle(handle: int, pid: int) -> str | None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        if kernel32.TerminateProcess(wintypes.HANDLE(handle), 124):
            return None
        error = ctypes.get_last_error()
        return f"TerminateProcess({pid}) failed with Windows error {error}."

    def terminate_and_wait(self, *, deadline: float) -> tuple[bool, str | None]:
        last_diagnostics: dict[int, str] = {}
        stable_empty_passes = 0
        while True:
            discovered, discovery_diagnostic = self.discover()
            if not discovered:
                stable_empty_passes = 0
                if time.monotonic() >= deadline:
                    closed, close_diagnostic = self.close()
                    return False, " ".join(
                        part
                        for part in (discovery_diagnostic, close_diagnostic)
                        if part
                    )
                time.sleep(min(0.025, max(0.001, deadline - time.monotonic())))
                continue

            active: list[_WindowsTrackedProcess] = []
            wait_failures: dict[int, str] = {}
            for tracked in self._tracked.values():
                running, wait_diagnostic = self._handle_is_running(tracked.handle)
                if running is True:
                    active.append(tracked)
                elif running is None:
                    wait_failures[tracked.pid] = wait_diagnostic or (
                        f"Could not verify process {tracked.pid}."
                    )

            if not active and not self._active_unverified and not wait_failures:
                stable_empty_passes += 1
                if stable_empty_passes >= 2:
                    closed, close_diagnostic = self.close()
                    return closed, close_diagnostic
            else:
                stable_empty_passes = 0
                for tracked in sorted(active, key=lambda item: item.depth):
                    terminate_diagnostic = self._terminate_handle(
                        tracked.handle,
                        tracked.pid,
                    )
                    if terminate_diagnostic is not None:
                        last_diagnostics[tracked.pid] = terminate_diagnostic
                last_diagnostics.update(wait_failures)
                last_diagnostics.update(self._active_unverified)

            if time.monotonic() >= deadline:
                remaining_pids = sorted(
                    {
                        *(item.pid for item in active),
                        *self._active_unverified,
                        *wait_failures,
                    }
                )
                details = [
                    last_diagnostics[pid]
                    for pid in remaining_pids
                    if pid in last_diagnostics
                ]
                summary = (
                    "Tracked Windows probe descendants did not exit within the cleanup "
                    f"budget: {remaining_pids}."
                )
                closed, close_diagnostic = self.close()
                return False, " ".join(
                    part
                    for part in (summary, *details, close_diagnostic)
                    if part
                )

            time.sleep(min(0.025, max(0.001, deadline - time.monotonic())))

    def close(self) -> tuple[bool, str | None]:
        if self._closed:
            return True, None

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        diagnostics: list[str] = []
        for tracked in self._tracked.values():
            if not kernel32.CloseHandle(wintypes.HANDLE(tracked.handle)):
                error = ctypes.get_last_error()
                diagnostics.append(
                    f"CloseHandle(tracked process {tracked.pid}) failed with Windows "
                    f"error {error}."
                )
        self._closed = True
        return not diagnostics, " ".join(diagnostics) or None


def _close_windows_job_handle(job_handle: int) -> tuple[bool, str | None]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    if kernel32.CloseHandle(wintypes.HANDLE(job_handle)):
        return True, None
    error = ctypes.get_last_error()
    return False, f"CloseHandle(job) failed with Windows error {error}."


def _configure_windows_job_kill_on_close(job_handle: int) -> tuple[bool, str | None]:
    import ctypes
    from ctypes import wintypes

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x00002000
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    configured = kernel32.SetInformationJobObject(
        wintypes.HANDLE(job_handle),
        job_object_extended_limit_information,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if configured:
        return True, None
    error = ctypes.get_last_error()
    return False, f"SetInformationJobObject failed with Windows error {error}."


def _attach_windows_probe_job(proc: subprocess.Popen[Any]) -> tuple[int | None, str | None]:
    """Attach a suspended probe target to a kill-on-close Job Object."""

    if os.name != "nt":
        return None, None

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    raw_job_handle = kernel32.CreateJobObjectW(None, None)
    if not raw_job_handle:
        error = ctypes.get_last_error()
        return None, f"CreateJobObjectW failed with Windows error {error}."
    job_handle = int(raw_job_handle)

    configured, configuration_diagnostic = _configure_windows_job_kill_on_close(
        job_handle
    )
    if not configured:
        _, close_diagnostic = _close_windows_job_handle(job_handle)
        parts = [
            part
            for part in (configuration_diagnostic, close_diagnostic)
            if part
        ]
        return None, " ".join(parts) or "Windows Job Object configuration failed."

    raw_process_handle = getattr(proc, "_handle", None)
    if raw_process_handle is None:
        _, close_diagnostic = _close_windows_job_handle(job_handle)
        parts = [
            "The suspended probe target did not expose a Windows process handle.",
            close_diagnostic,
        ]
        return None, " ".join(part for part in parts if part)

    assigned = kernel32.AssignProcessToJobObject(
        wintypes.HANDLE(job_handle),
        wintypes.HANDLE(int(raw_process_handle)),
    )
    if not assigned:
        error = ctypes.get_last_error()
        _, close_diagnostic = _close_windows_job_handle(job_handle)
        parts = [
            f"AssignProcessToJobObject failed with Windows error {error}.",
            close_diagnostic,
        ]
        return None, " ".join(part for part in parts if part)

    return job_handle, None


def _resume_windows_suspended_process(
    proc: subprocess.Popen[Any],
) -> tuple[bool, str | None]:
    """Resume the primary thread created by ``CREATE_SUSPENDED``."""

    if os.name != "nt":
        return True, None

    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    th32cs_snapthread = 0x00000004
    thread_suspend_resume = 0x0002
    resume_failed = 0xFFFFFFFF
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    raw_snapshot_handle = kernel32.CreateToolhelp32Snapshot(th32cs_snapthread, 0)
    if not raw_snapshot_handle or int(raw_snapshot_handle) == invalid_handle_value:
        error = ctypes.get_last_error()
        return False, f"CreateToolhelp32Snapshot failed with Windows error {error}."
    snapshot_handle = int(raw_snapshot_handle)

    resumed = False
    diagnostic: str | None = None
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        has_entry = kernel32.Thread32First(
            wintypes.HANDLE(snapshot_handle),
            ctypes.byref(entry),
        )
        while has_entry:
            if int(entry.th32OwnerProcessID) == proc.pid:
                raw_thread_handle = kernel32.OpenThread(
                    thread_suspend_resume,
                    False,
                    entry.th32ThreadID,
                )
                if not raw_thread_handle:
                    error = ctypes.get_last_error()
                    diagnostic = (
                        "OpenThread for the suspended probe target failed with Windows "
                        f"error {error}."
                    )
                    break
                thread_handle = int(raw_thread_handle)
                previous_suspend_count = kernel32.ResumeThread(
                    wintypes.HANDLE(thread_handle)
                )
                resume_error = (
                    ctypes.get_last_error()
                    if previous_suspend_count == resume_failed
                    else None
                )
                thread_closed = kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
                close_error = ctypes.get_last_error() if not thread_closed else None
                if previous_suspend_count == resume_failed:
                    diagnostic = (
                        "ResumeThread for the suspended probe target failed with Windows "
                        f"error {resume_error}."
                    )
                elif previous_suspend_count != 1:
                    diagnostic = (
                        "The suspended probe target had an unexpected primary-thread suspend "
                        f"count of {previous_suspend_count}."
                    )
                elif not thread_closed:
                    diagnostic = (
                        "CloseHandle(thread) failed after the probe target resumed with "
                        f"Windows error {close_error}."
                    )
                else:
                    resumed = True
                break
            has_entry = kernel32.Thread32Next(
                wintypes.HANDLE(snapshot_handle),
                ctypes.byref(entry),
            )
        if not resumed and diagnostic is None:
            diagnostic = "The suspended probe target's primary thread could not be found."
    finally:
        snapshot_closed = kernel32.CloseHandle(wintypes.HANDLE(snapshot_handle))
        if not snapshot_closed:
            error = ctypes.get_last_error()
            close_diagnostic = (
                f"CloseHandle(thread snapshot) failed with Windows error {error}."
            )
            diagnostic = " ".join(
                part for part in (diagnostic, close_diagnostic) if part
            )
            resumed = False

    return resumed, diagnostic


def _terminate_windows_job_processes(job_handle: int) -> tuple[bool, str | None]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL

    if kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 124):
        return True, None
    error = ctypes.get_last_error()
    return False, f"TerminateJobObject failed with Windows error {error}."


def _terminate_windows_probe_job(job_handle: int) -> tuple[bool, str | None]:
    terminated, termination_diagnostic = _terminate_windows_job_processes(job_handle)
    closed, close_diagnostic = _close_windows_job_handle(job_handle)
    if terminated and closed:
        return True, None
    if not terminated and closed:
        diagnostic = " ".join(
            part
            for part in (
                termination_diagnostic,
                "The kill-on-job-close fallback was invoked.",
            )
            if part
        )
        return True, diagnostic
    return False, " ".join(
        part for part in (termination_diagnostic, close_diagnostic) if part
    ) or None


def _bounded_taskkill_process_tree(
    proc: subprocess.Popen[Any],
    *,
    deadline: float,
) -> tuple[bool, str | None]:
    """Best-effort Windows fallback when Job Object assignment is unavailable."""

    if proc.poll() is not None:
        return False, "The root process exited before its descendant tree was contained."

    killer: subprocess.Popen[Any] | None = None
    try:
        killer = subprocess.Popen(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        remaining = max(0.001, deadline - time.monotonic())
        try:
            returncode = killer.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            killer.kill()
            remaining = max(0.001, deadline - time.monotonic())
            try:
                killer.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
            return False, "taskkill did not finish within the probe cleanup budget."
        if returncode == 0:
            return True, None
        return False, f"taskkill exited with code {returncode}."
    except OSError as exc:
        return False, f"taskkill could not be launched: {exc}"
    finally:
        if killer is not None and killer.poll() is None:
            killer.kill()


def _wait_for_bounded_probe_completion(
    proc: subprocess.Popen[Any],
    *,
    timeout_seconds: float,
    windows_tree_tracker: _WindowsProcessTreeTracker | None,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if windows_tree_tracker is not None:
            # Retaining descendant handles while the wrapper is alive preserves lineage even
            # after short-lived redirectors and intermediate interpreters have exited.
            windows_tree_tracker.discover()

        if proc.poll() is not None:
            if windows_tree_tracker is not None:
                windows_tree_tracker.discover()
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if windows_tree_tracker is not None:
                windows_tree_tracker.discover()
            return True

        try:
            proc.wait(timeout=min(0.025, remaining))
        except subprocess.TimeoutExpired:
            continue


def _cleanup_bounded_probe_tree(
    proc: subprocess.Popen[Any],
    *,
    windows_job_handle: int | None,
    windows_tree_tracker: _WindowsProcessTreeTracker | None,
    containment_diagnostic: str | None,
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + _PROBE_TREE_CLEANUP_BUDGET_SECONDS
    cleanup_succeeded = False
    cleanup_diagnostic = containment_diagnostic

    if os.name == "nt":
        tree_tracking_prepared = True
        tree_tracking_preparation_diagnostic: str | None = None
        if windows_tree_tracker is not None:
            # Take the last complete snapshot before any contained process is terminated. The
            # tracker retains handles and parent identities, so later cleanup does not depend on
            # the root wrapper still being alive.
            tree_tracking_prepared = False
            for attempt in range(3):
                discovered, discovery_diagnostic = windows_tree_tracker.discover()
                if discovered:
                    tree_tracking_prepared = True
                    tree_tracking_preparation_diagnostic = None
                    break
                tree_tracking_preparation_diagnostic = discovery_diagnostic
                if attempt < 2 and time.monotonic() < deadline:
                    time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
        if windows_job_handle is not None:
            cleanup_succeeded, job_cleanup_diagnostic = _terminate_windows_probe_job(
                windows_job_handle
            )
            cleanup_diagnostic = " ".join(
                part
                for part in (containment_diagnostic, job_cleanup_diagnostic)
                if part
            ) or None
            if not cleanup_succeeded:
                fallback_succeeded, fallback_diagnostic = _bounded_taskkill_process_tree(
                    proc,
                    deadline=deadline,
                )
                cleanup_succeeded = fallback_succeeded
                cleanup_diagnostic = " ".join(
                    part
                    for part in (cleanup_diagnostic, fallback_diagnostic)
                    if part
                ) or None
        else:
            cleanup_succeeded, fallback_diagnostic = _bounded_taskkill_process_tree(
                proc,
                deadline=deadline,
            )
            parts = [part for part in (containment_diagnostic, fallback_diagnostic) if part]
            cleanup_diagnostic = " ".join(parts) or None

        if windows_tree_tracker is not None:
            tree_cleanup_succeeded, tree_cleanup_diagnostic = (
                windows_tree_tracker.terminate_and_wait(deadline=deadline)
            )
            cleanup_succeeded = (
                cleanup_succeeded
                and tree_tracking_prepared
                and tree_cleanup_succeeded
            )
            cleanup_diagnostic = " ".join(
                part
                for part in (
                    cleanup_diagnostic,
                    tree_tracking_preparation_diagnostic,
                    tree_cleanup_diagnostic,
                )
                if part
            ) or None
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            cleanup_succeeded = True
            cleanup_diagnostic = None
        except ProcessLookupError:
            # No process remains in the probe's dedicated process group.
            cleanup_succeeded = True
            cleanup_diagnostic = None
        except OSError as exc:
            cleanup_diagnostic = f"Could not terminate probe process group: {exc}"

    if proc.poll() is None:
        remaining = max(0.001, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            remaining = max(0.001, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                cleanup_succeeded = False
                tail = "The root probe process did not exit within the cleanup budget."
                cleanup_diagnostic = " ".join(
                    part for part in (cleanup_diagnostic, tail) if part
                )

    return cleanup_succeeded, cleanup_diagnostic


def _read_probe_output(handle: Any) -> str:
    handle.seek(0)
    raw = handle.read(_PROBE_OUTPUT_CAPTURE_LIMIT_BYTES + 1)
    truncated = len(raw) > _PROBE_OUTPUT_CAPTURE_LIMIT_BYTES
    captured = raw[:_PROBE_OUTPUT_CAPTURE_LIMIT_BYTES]
    if captured.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = captured.decode("utf-16", errors="replace")
    else:
        even_nuls = captured[::2].count(0)
        odd_nuls = captured[1::2].count(0)
        if odd_nuls > max(4, even_nuls * 2):
            decoded = captured.decode("utf-16-le", errors="replace")
        elif even_nuls > max(4, odd_nuls * 2):
            decoded = captured.decode("utf-16-be", errors="replace")
        else:
            decoded = captured.decode("utf-8", errors="replace")
    if truncated:
        decoded += "\n[probe output truncated]"
    return decoded


def _run_bounded_command_probe(
    argv: list[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> _BoundedCommandProbeResult:
    """
    Run a small usability probe without relying on ``Popen.communicate``.

    Python's timeout cleanup for captured pipes kills only the immediate process and then drains
    those pipes without another deadline. A descendant that inherited stdout or stderr can make
    that drain permanent. Temporary files make output collection non-blocking, while a Windows
    Job Object or POSIX process group gives cleanup ownership of the complete launched tree. On
    Windows, the actual target is created suspended, assigned to the Job Object, and only then
    resumed, so a failed Job Object attachment cannot race with target or descendant startup.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED
        )
    else:
        popen_kwargs["start_new_session"] = True

    with ExitStack() as stack:
        stdout_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
        stderr_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
        proc = subprocess.Popen(
            argv,
            stdout=stdout_file,
            stderr=stderr_file,
            **popen_kwargs,
        )
        windows_job_handle: int | None = None
        windows_tree_tracker: _WindowsProcessTreeTracker | None = None
        containment_diagnostic: str | None = None
        if os.name == "nt":
            windows_job_handle, containment_diagnostic = _attach_windows_probe_job(proc)
            if windows_job_handle is None:
                containment_diagnostic = containment_diagnostic or (
                    "Windows probe containment could not be established."
                )
                cleanup_succeeded, cleanup_diagnostic = _cleanup_bounded_probe_tree(
                    proc,
                    windows_job_handle=None,
                    windows_tree_tracker=None,
                    containment_diagnostic=containment_diagnostic,
                )
                stdout = _read_probe_output(stdout_file)
                captured_stderr = _read_probe_output(stderr_file).strip()
                failure = (
                    "Probe target was not launched because Windows containment could not be "
                    f"established: {containment_diagnostic}"
                )
                stderr = "\n".join(part for part in (captured_stderr, failure) if part)
                return _BoundedCommandProbeResult(
                    returncode=_PROBE_CONTAINMENT_FAILURE_EXIT_CODE,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=False,
                    cleanup_succeeded=cleanup_succeeded,
                    cleanup_diagnostic=cleanup_diagnostic,
                )

            windows_tree_tracker = _WindowsProcessTreeTracker(proc.pid)
            stack.callback(windows_tree_tracker.close)
            resumed, resume_diagnostic = _resume_windows_suspended_process(proc)
            if not resumed:
                containment_diagnostic = (
                    "The suspended Windows probe target could not be resumed after "
                    f"containment: {resume_diagnostic or 'unknown resume failure'}"
                )
                cleanup_succeeded, cleanup_diagnostic = _cleanup_bounded_probe_tree(
                    proc,
                    windows_job_handle=windows_job_handle,
                    windows_tree_tracker=windows_tree_tracker,
                    containment_diagnostic=containment_diagnostic,
                )
                stdout = _read_probe_output(stdout_file)
                captured_stderr = _read_probe_output(stderr_file).strip()
                failure = f"Probe target was not run: {containment_diagnostic}"
                stderr = "\n".join(part for part in (captured_stderr, failure) if part)
                return _BoundedCommandProbeResult(
                    returncode=_PROBE_CONTAINMENT_FAILURE_EXIT_CODE,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=False,
                    cleanup_succeeded=cleanup_succeeded,
                    cleanup_diagnostic=cleanup_diagnostic,
                )

        timed_out = _wait_for_bounded_probe_completion(
            proc,
            timeout_seconds=timeout_seconds,
            windows_tree_tracker=windows_tree_tracker,
        )

        completed_returncode = proc.returncode
        cleanup_succeeded, cleanup_diagnostic = _cleanup_bounded_probe_tree(
            proc,
            windows_job_handle=windows_job_handle,
            windows_tree_tracker=windows_tree_tracker,
            containment_diagnostic=containment_diagnostic,
        )
        stdout = _read_probe_output(stdout_file)
        stderr = _read_probe_output(stderr_file)

    return _BoundedCommandProbeResult(
        returncode=124 if timed_out else int(completed_returncode or 0),
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        cleanup_succeeded=cleanup_succeeded,
        cleanup_diagnostic=cleanup_diagnostic,
    )


def _probe_commands_local(
    commands: list[str],
    *,
    workspace_dir: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    out: dict[str, bool] = {}
    probe_details: dict[str, dict[str, Any]] = {}
    effective_env: dict[str, str] | None = None
    effective_path: str | None = None
    if env_overrides:
        effective_env = dict(os.environ)
        for key, value in env_overrides.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            effective_env[key] = value
        effective_path = env_overrides.get("PATH")
    python_commands = [cmd for cmd in commands if cmd in {"python", "python3", "py"}]
    python_probe = (
        resolve_usable_python_interpreter(
            workspace_dir=workspace_dir,
            candidate_commands=python_commands,
            timeout_seconds=_PYTHON_INTERPRETER_PROBE_BUDGET_SECONDS,
            path=effective_path,
        )
        if python_commands
        else None
    )
    python_by_command = python_probe.by_command() if python_probe is not None else {}
    for cmd in commands:
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        if cmd in python_by_command:
            candidate = python_by_command[cmd]
            out[cmd] = bool(candidate.usable)
            probe_details[cmd] = candidate.to_dict()
            continue

        resolved = (
            shutil.which(cmd, path=effective_path)
            if effective_path is not None
            else shutil.which(cmd)
        )
        present = resolved is not None
        usable = present
        reason_code: str | None = None if present else "not_found"
        reason: str | None = None if present else f"`{cmd}` was not found on PATH."

        if resolved is not None and cmd in {"pdm"}:
            # Some environments can resolve `pdm` but block execution or hang at import time.
            try:
                probe = _run_bounded_command_probe(
                    [resolved, "--version"],
                    timeout_seconds=_PDM_VERSION_PROBE_BUDGET_SECONDS,
                    env=effective_env,
                )
                usable = (
                    not probe.timed_out
                    and probe.returncode == 0
                    and probe.cleanup_succeeded
                )
                probe_details[cmd] = {
                    "command": cmd,
                    "resolved_path": resolved,
                    "present": present,
                    "usable": bool(usable),
                    "probe_argv": [resolved, "--version"],
                    "probe_exit_code": probe.returncode,
                    "probe_stdout_excerpt": probe.stdout.strip()[:300] or None,
                    "probe_stderr_excerpt": probe.stderr.strip()[:300] or None,
                    "probe_timed_out": probe.timed_out,
                    "probe_tree_cleanup_succeeded": probe.cleanup_succeeded,
                    "probe_tree_cleanup_diagnostic": probe.cleanup_diagnostic,
                }
                if probe.timed_out:
                    reason_code = "unresponsive"
                    reason = "pdm probe timed out (2.5s) running `pdm --version`."
                elif not probe.cleanup_succeeded:
                    reason_code = "probe_cleanup_failed"
                    reason = (
                        "pdm probe completed, but descendant-process cleanup could not be "
                        "verified"
                        + (
                            f": {probe.cleanup_diagnostic}"
                            if probe.cleanup_diagnostic
                            else "."
                        )
                    )
                elif not usable:
                    reason_code = "probe_failed"
                    details_parts = [
                        probe.stderr.strip(),
                        probe.stdout.strip(),
                    ]
                    details = "; ".join([p for p in details_parts if p]) or (
                        f"exit_code={probe.returncode}"
                    )
                    reason = f"pdm probe exited non-zero: {details}"
            except OSError as e:
                usable = False
                reason_code = "blocked"
                reason = f"pdm probe failed: {e}"
            if cmd in probe_details:
                probe_details[cmd]["reason_code"] = reason_code
                probe_details[cmd]["reason"] = reason

        if cmd == "bash" and os.name == "nt" and resolved is not None:
            # On some Windows sandboxes, bash.exe may be on PATH (e.g., Git Bash) but execution is
            # blocked by policy ("Access is denied"). Probe by actually starting bash.
            try:
                probe = _run_bounded_command_probe(
                    [resolved, "-lc", "echo ok"],
                    timeout_seconds=_BASH_LAUNCH_PROBE_BUDGET_SECONDS,
                    env=effective_env,
                )
                usable = (
                    not probe.timed_out
                    and probe.returncode == 0
                    and probe.cleanup_succeeded
                )
                probe_details[cmd] = {
                    "command": cmd,
                    "resolved_path": resolved,
                    "present": present,
                    "usable": bool(usable),
                    "probe_argv": [resolved, "-lc", "echo ok"],
                    "probe_exit_code": probe.returncode,
                    "probe_stdout_excerpt": probe.stdout.strip()[:300] or None,
                    "probe_stderr_excerpt": probe.stderr.strip()[:300] or None,
                    "probe_timed_out": probe.timed_out,
                    "probe_tree_cleanup_succeeded": probe.cleanup_succeeded,
                    "probe_tree_cleanup_diagnostic": probe.cleanup_diagnostic,
                }
                if probe.timed_out:
                    reason_code = "unresponsive"
                    reason = 'bash probe timed out (2.0s) running `bash -lc "echo ok"`.'
                elif not probe.cleanup_succeeded:
                    reason_code = "probe_cleanup_failed"
                    reason = (
                        "bash probe completed, but descendant-process cleanup could not be "
                        "verified"
                        + (
                            f": {probe.cleanup_diagnostic}"
                            if probe.cleanup_diagnostic
                            else "."
                        )
                    )
                elif not usable:
                    reason_code = "probe_failed"
                    stderr = probe.stderr.strip()
                    stdout = probe.stdout.strip()
                    reason = "bash probe exited non-zero" + (
                        f": {stderr or stdout}"
                        if stderr or stdout
                        else f" (exit_code={probe.returncode})"
                    )
            except OSError as e:
                usable = False
                reason_code = "blocked"
                reason = f"bash probe failed: {e}"
            if cmd in probe_details:
                probe_details[cmd]["reason_code"] = reason_code
                probe_details[cmd]["reason"] = reason

        out[cmd] = bool(usable)
        probe_details.setdefault(
            cmd,
            {
                "command": cmd,
                "resolved_path": resolved,
                "present": present,
                "usable": bool(usable),
                "reason_code": reason_code,
                "reason": reason,
            },
        )

    meta: dict[str, Any] = {"command_probe_details": probe_details}
    meta["shell_probe"] = _probe_local_shell_payload(
        workspace_dir=workspace_dir,
        env=effective_env,
    )
    if python_probe is not None:
        meta["python_interpreter"] = python_probe.to_dict()
    return out, meta


def _probe_local_shell_payload(
    *,
    workspace_dir: Path | None,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    """
    Launch a payload-equivalent no-op through the local shell backend.

    Command discovery alone is not enough to prove shell capability: process creation can still be
    blocked by sandbox or host policy.  This probe is deliberately tiny, bounded, and records the
    same canonical shape as the container probe so the shell capability resolver can distinguish
    "command exists" from "payload launch works".
    """

    if os.name == "nt":
        resolved = shutil.which("powershell", path=(env or os.environ).get("PATH"))
        if resolved is None:
            resolved = shutil.which("pwsh", path=(env or os.environ).get("PATH"))
        if resolved is None:
            return {
                "kind": "backend_shell_payload",
                "shell_family": "powershell",
                "exit_code": 1,
                "stdout": "",
                "stderr": "PowerShell executable was not found for local shell payload probe.",
                "reason_code": "not_found",
            }
        argv = [
            resolved,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Write-Output 'shell_probe=ok'",
        ]
        shell_family = "powershell"
    else:
        resolved = shutil.which("sh", path=(env or os.environ).get("PATH")) or "sh"
        argv = [resolved, "-lc", "printf 'shell_probe=ok\\n'"]
        shell_family = "sh"

    try:
        probe = _run_bounded_command_probe(
            argv,
            cwd=workspace_dir,
            timeout_seconds=_LOCAL_SHELL_PAYLOAD_PROBE_BUDGET_SECONDS,
            env=env,
        )
    except OSError as e:
        return {
            "kind": "backend_shell_payload",
            "shell_family": shell_family,
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Local shell payload probe failed to launch: {e}",
            "reason_code": "blocked",
            "probe_argv": argv,
        }

    if probe.timed_out:
        return {
            "kind": "backend_shell_payload",
            "shell_family": shell_family,
            "exit_code": 124,
            "stdout": probe.stdout.strip()[:300],
            "stderr": probe.stderr.strip()[:300] or "Local shell payload probe timed out.",
            "reason_code": "unresponsive",
            "probe_argv": argv,
            "probe_tree_cleanup_succeeded": probe.cleanup_succeeded,
            "probe_tree_cleanup_diagnostic": probe.cleanup_diagnostic,
        }

    if not probe.cleanup_succeeded:
        cleanup_reason = (
            "Local shell payload completed, but descendant-process cleanup could not be "
            "verified"
            + (
                f": {probe.cleanup_diagnostic}"
                if probe.cleanup_diagnostic
                else "."
            )
        )
        return {
            "kind": "backend_shell_payload",
            "shell_family": shell_family,
            "exit_code": _PROBE_CONTAINMENT_FAILURE_EXIT_CODE,
            "stdout": probe.stdout.strip()[:300],
            "stderr": cleanup_reason,
            "reason_code": "probe_cleanup_failed",
            "probe_argv": argv,
            "probe_tree_cleanup_succeeded": False,
            "probe_tree_cleanup_diagnostic": probe.cleanup_diagnostic,
        }

    stdout = probe.stdout.strip()
    stderr = probe.stderr.strip()
    marker_seen = "shell_probe=ok" in stdout.splitlines()
    return {
        "kind": "backend_shell_payload",
        "shell_family": shell_family,
        "exit_code": probe.returncode if marker_seen else 1,
        "stdout": "shell_probe=ok" if marker_seen else stdout[:300],
        "stderr": (
            stderr[:300]
            if marker_seen
            else (stderr[:300] or "Local shell payload probe did not emit sentinel output.")
        ),
        "probe_argv": argv,
        "probe_tree_cleanup_succeeded": probe.cleanup_succeeded,
        "probe_tree_cleanup_diagnostic": probe.cleanup_diagnostic,
    }


def _format_windows_python_preflight_error(probe: Any) -> str:
    payload = probe.to_dict() if hasattr(probe, "to_dict") else {}
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    candidates_list = candidates if isinstance(candidates, list) else []
    lines = [
        "Python preflight failed on Windows: no usable interpreter could be resolved within ~5s.",
        "",
        "Tried:",
    ]
    for item in candidates_list:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        resolved_path = item.get("resolved_path")
        reason_code = item.get("reason_code")
        reason = item.get("reason")
        summary = f"{command} -> {resolved_path} ({reason_code})"
        lines.append("  - " + summary)
        if isinstance(reason, str) and reason.strip():
            tail = reason.strip()
            if len(tail) > 300:
                tail = tail[:300].rstrip() + "…"
            lines.append("      " + tail.replace("\n", "\n      "))
    lines.extend(
        [
            "",
            "Fix options:",
            "  1) Install CPython (python.org) or via winget: "
            "winget install -e --id Python.Python.3.13",
            "  2) Disable App Execution Alias shims: Settings -> Apps -> Advanced app settings -> "
            "App execution aliases -> turn off python.exe/python3.exe",
            "  3) Use a portable/vendored Python and put its folder first on PATH "
            "(or use --exec-backend docker)",
        ]
    )
    return "\n".join(lines)


def _ensure_windows_python_on_path(
    *,
    workspace_dir: Path,
    env_overrides: dict[str, str] | None,
) -> dict[str, str]:
    base = dict(env_overrides or {})
    probe = resolve_usable_python_interpreter(
        workspace_dir=workspace_dir,
        candidate_commands=("python", "python3", "py"),
        timeout_seconds=_PYTHON_INTERPRETER_PROBE_BUDGET_SECONDS,
        include_sys_executable=True,
    )
    if probe.selected_command is None:
        raise RuntimeError(_format_windows_python_preflight_error(probe))

    python_exe = probe.selected_executable or probe.selected_resolved_path or ""
    python_exe_s = python_exe.strip()
    if python_exe_s:
        base.setdefault("USERTEST_PYTHON", python_exe_s)
        python_dir = str(Path(python_exe_s).parent)
        prior_path = base.get("PATH", os.environ.get("PATH", ""))
        if prior_path:
            base["PATH"] = f"{python_dir}{os.pathsep}{prior_path}"
        else:
            base["PATH"] = python_dir
    return base

__all__ = (
    "_BASE_PREFLIGHT_COMMANDS",
    "_agent_binary_for_preflight_probe",
    "_build_preflight_command_list",
    "_ensure_windows_python_on_path",
    "_format_windows_python_preflight_error",
    "_probe_commands_local",
    "_probe_local_shell_payload",
)
