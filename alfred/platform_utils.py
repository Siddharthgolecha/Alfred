"""Cross-platform utility helpers for Alfred.

Abstracts OS-specific behaviour so the rest of the codebase can import
from here rather than sprinkling ``sys.platform`` checks everywhere.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
_WINDOWS_CPU_LOCK = threading.Lock()
_WINDOWS_CPU_BASELINE = None
_WINDOWS_CPU_SAMPLED_AT = 0.0
_WINDOWS_CPU_LOAD = None

# AF_UNIX is unavailable on Windows before Python 3.9 / Windows 10 1803.
HAS_AF_UNIX: bool = hasattr(__import__("socket"), "AF_UNIX")

# ---------------------------------------------------------------------------
# Default XDG / platform data directories
# ---------------------------------------------------------------------------

def _default_data_dir() -> str:
    """Return the user-scoped data directory appropriate for the current OS."""
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return str(Path(base) / "Alfred")
    if IS_MACOS:
        return str(Path("~/Library/Application Support/Alfred").expanduser())
    # Linux / other POSIX: follow XDG Base Directory spec.
    base = os.environ.get("XDG_DATA_HOME") or str(Path("~/.local/share").expanduser())
    return str(Path(base) / "alfred")


def _default_state_dir() -> str:
    """Return the user-scoped log/state directory appropriate for the current OS."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return str(Path(base) / "Alfred" / "logs")
    if IS_MACOS:
        return str(Path("~/Library/Logs/Alfred").expanduser())
    # Linux / other POSIX: follow XDG state spec.
    base = os.environ.get("XDG_STATE_HOME") or str(Path("~/.local/state").expanduser())
    return str(Path(base) / "alfred")


def _windows_cpu_load() -> float:
    """Measure Windows CPU use over 50ms, then reuse samples for 250ms."""
    global _WINDOWS_CPU_BASELINE, _WINDOWS_CPU_SAMPLED_AT, _WINDOWS_CPU_LOAD
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    def read_times():
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError("GetSystemTimes failed")
        value = lambda item: (int(item.dwHighDateTime) << 32) | int(item.dwLowDateTime)
        return value(idle), value(kernel), value(user)

    with _WINDOWS_CPU_LOCK:
        now = time.monotonic()
        if _WINDOWS_CPU_BASELINE is not None and now - _WINDOWS_CPU_SAMPLED_AT < 0.25:
            return _WINDOWS_CPU_LOAD
        before = _WINDOWS_CPU_BASELINE or read_times()
        if _WINDOWS_CPU_BASELINE is None:
            time.sleep(0.05)
        after = read_times()
        total = (after[1] - before[1]) + (after[2] - before[2])
        busy = total - (after[0] - before[0])
        _WINDOWS_CPU_LOAD = max(0.0, min(1.0, busy / total)) if total else 0.0
        _WINDOWS_CPU_BASELINE = after
        _WINDOWS_CPU_SAMPLED_AT = time.monotonic()
        return _WINDOWS_CPU_LOAD


# ---------------------------------------------------------------------------
# Process group / signal utilities
# ---------------------------------------------------------------------------

def terminate_process_group(process) -> None:
    """Send SIGTERM (or TerminateProcess on Windows) to *process* and its group."""
    if process.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            process.terminate()
        else:
            import signal
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
    except OSError:
        pass


def kill_process_group(process) -> None:
    """Send SIGKILL (or TerminateProcess on Windows) to *process* and its group."""
    if process.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            process.kill()
        else:
            import signal
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Resource / memory sampling
# ---------------------------------------------------------------------------

def sample_resources() -> dict:
    """Return a best-effort snapshot of system resource pressure.

    Returns a dict with keys ``available_mib``, ``swap_used_mib``, and
    ``cpu_load_per_core`` (all ``float | None``).  Unknown values are ``None``.
    """
    _NA = {"available_mib": None, "swap_used_mib": None, "cpu_load_per_core": None}

    available_mib = swap_used_mib = cpu_load = None

    if IS_LINUX:
        try:
            info: dict[str, float] = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                name, _, rest = line.partition(":")
                if name in ("MemAvailable", "SwapTotal", "SwapFree", "MemTotal"):
                    info[name] = int(rest.strip().split()[0]) / 1024.0
            available_mib = info.get("MemAvailable")
            if "SwapTotal" in info and "SwapFree" in info:
                swap_used_mib = max(0.0, info["SwapTotal"] - info["SwapFree"])
        except (OSError, ValueError, IndexError):
            return _NA
    elif IS_MACOS:
        try:
            # vm_stat outputs page counts; page size is typically 4096 B.
            out = subprocess.check_output(["vm_stat"], text=True, timeout=3)
            page_size = 4096
            pages: dict[str, int] = {}
            for line in out.splitlines():
                for key in ("Pages free", "Pages inactive", "Pages wired down",
                            "Pages active", "Swapouts"):
                    if line.startswith(key + ":"):
                        try:
                            pages[key] = int(line.split(":")[1].strip().rstrip("."))
                        except ValueError:
                            pass
            free = pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
            available_mib = free * page_size / (1024 * 1024)
            swap = subprocess.check_output(
                ["sysctl", "-n", "vm.swapusage"], text=True, timeout=3
            )
            import re
            match = re.search(r"used\s*=\s*([0-9.]+)M", swap)
            if match:
                swap_used_mib = float(match.group(1))
        except (OSError, subprocess.SubprocessError, ValueError):
            return _NA
    elif IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            memory = MEMORYSTATUSEX()
            memory.dwLength = ctypes.sizeof(memory)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                return _NA
            available_mib = memory.ullAvailPhys / (1024 * 1024)
            # Commit beyond physical RAM is the memory pressure that can page to disk.
            swap_used_mib = max(0.0, (memory.ullTotalPageFile - memory.ullAvailPageFile
                                      - memory.ullTotalPhys) / (1024 * 1024))

            cpu_load = _windows_cpu_load()
        except (OSError, AttributeError, ValueError):
            return _NA

    if cpu_load is None:
        try:
            count = os.cpu_count() or 1
            cpu_load = os.getloadavg()[0] / max(1, count)
        except (OSError, AttributeError):
            cpu_load = None  # All required fields are checked before inference admission.

    return {
        "available_mib": available_mib,
        "swap_used_mib": swap_used_mib,
        "cpu_load_per_core": cpu_load,
    }


# ---------------------------------------------------------------------------
# Uptime
# ---------------------------------------------------------------------------

def system_uptime_seconds() -> int:
    """Return approximate system uptime in seconds, or 0 if unavailable."""
    if IS_LINUX:
        try:
            return round(float(Path("/proc/uptime").read_text().split()[0]))
        except (OSError, ValueError, IndexError):
            return 0
    if IS_MACOS:
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"], text=True, timeout=3
            )
            # kern.boottime looks like:  { sec = 1700000000, usec = 0 } ...
            import re
            m = re.search(r"sec\s*=\s*(\d+)", out)
            if m:
                return max(0, int(time.time()) - int(m.group(1)))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return 0
