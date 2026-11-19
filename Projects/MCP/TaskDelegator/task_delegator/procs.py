"""Process helpers. Windows-first, with a POSIX fallback so the code still imports elsewhere."""

from __future__ import annotations

import os
import subprocess

IS_WINDOWS = os.name == "nt"

# Popen creation flags: no console window, own process group (so a tree kill is clean).
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
POPEN_FLAGS = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WINDOWS else 0

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    def _creation_time(handle) -> int | None:
        ft = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(f) for f in ft)):
            return None
        return (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime


def process_created(pid: int) -> int | None:
    """Creation timestamp of a live process (Windows FILETIME), or None.

    Together with the PID it identifies one specific process: a later process that
    happens to get the same PID has a different creation time.
    """
    if not IS_WINDOWS or not pid:
        return None
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        return _creation_time(handle)
    finally:
        _k32.CloseHandle(handle)


def pid_alive(pid: int | None, expect_image: str | None = None, created: int | None = None) -> bool:
    """True if `pid` is still the same running process we launched.

    `created` (from process_created at launch) is the reliable guard against PID reuse;
    `expect_image` (exe basename) is a weaker fallback for jobs recorded without it.
    """
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return False
        if created is not None:
            return _creation_time(handle) == created
        if expect_image:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower() == expect_image.lower()
            return False
        return True
    finally:
        _k32.CloseHandle(handle)


def run_capture(argv: list[str], timeout: float, env: dict[str, str] | None = None,
                text: bool = False) -> subprocess.CompletedProcess:
    """Like subprocess.run(capture_output=True, timeout=...), but a timeout kills the whole tree.

    subprocess.run kills only the direct child: a script that started a process of its own (the
    Codex usage script drives `codex app-server`) would leave that one running. Raises
    subprocess.TimeoutExpired after the tree is gone.
    """
    kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "stdin": subprocess.DEVNULL, "env": env}
    if IS_WINDOWS:
        kw["creationflags"] = CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True  # its own process group, so kill_tree takes the children too
    if text:
        kw.update(text=True, encoding="utf-8", errors="replace")
    proc = subprocess.Popen(argv, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def kill_tree(pid: int) -> tuple[bool, str]:
    """Kill a process and all its children. Returns (ok, message)."""
    if IS_WINDOWS:
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="oem",  # taskkill prints in the console code page (e.g. cp866)
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        msg = (r.stdout or r.stderr or "").strip()
        return r.returncode == 0, msg
    try:
        os.killpg(os.getpgid(pid), 9)
        return True, "killed"
    except OSError as e:
        return False, str(e)
