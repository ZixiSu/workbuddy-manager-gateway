"""Start the installed app only when it is not running; never terminate it.

The launcher is a preview by default. Starting is a separate explicit action,
and a running application is never closed, killed or reconfigured: the user is
told to restart WorkBuddy normally so the local entry is enabled.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .desktop import DesktopConnection, DesktopUnavailable


def running_processes(executable: str | Path) -> list[int]:
    """PIDs whose image is exactly the configured executable (Windows only)."""
    if os.name != "nt":
        raise DesktopUnavailable("Process ownership inspection requires Windows; other platforms are not claimed as supported")
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    expected = os.path.normcase(os.path.realpath(str(executable)))
    found = []
    for _ in range(4):
        capacity = 4096
        buffer = (wintypes.DWORD * capacity)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            raise DesktopUnavailable("Unable to enumerate local processes")
        count = min(needed.value // ctypes.sizeof(wintypes.DWORD), capacity)
        if count < capacity:
            for index in range(count):
                pid = int(buffer[index])
                if not pid:
                    continue
                handle = kernel.OpenProcess(0x1000, False, pid)
                if not handle:
                    continue
                try:
                    image = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(image))
                    if kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
                        if os.path.normcase(os.path.realpath(image.value)) == expected:
                            found.append(pid)
                finally:
                    kernel.CloseHandle(handle)
            return sorted(set(found))
    raise DesktopUnavailable("Process list kept changing; try again")


def launch(connection: DesktopConnection, execute: bool = False) -> dict:
    processes = running_processes(connection.executable)
    plan = {
        "executable": connection.executable,
        "port": connection.port,
        "host": connection.host,
        "arguments": list(connection.arguments),
        "process_environment": {"WORKBUDDY_REMOTE_DEBUGGING_PORT": str(connection.port)},
        "persistent_system_changes": False,
        "requires": "Starting the installed app in the background. An already running app is never restarted or killed.",
    }
    if processes:
        return {**plan, "running": True, "launched": False, "pids": processes,
                "next_step": "WorkBuddy is already running. If the local entry is not enabled, quit it from its own "
                             "menu and start it again normally; this launcher will not terminate or restart a running app."}
    if not execute:
        return {**plan, "running": False, "launched": False, "preview": True,
                "next_step": "Re-run with --execute-launch to start the installed app in the background."}
    environment = os.environ.copy()
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    environment.pop("NODE_OPTIONS", None)
    environment["WORKBUDDY_REMOTE_DEBUGGING_PORT"] = str(connection.port)
    log_directory = Path(connection.executable).parent
    process = subprocess.Popen(
        [connection.executable, *connection.arguments], cwd=str(log_directory), env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    return {**plan, "running": False, "launched": True, "pid": process.pid,
            "note": "The app was started in the background; the local entry becomes available after it finishes loading."}
