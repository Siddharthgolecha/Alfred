"""Cross-platform utility helpers for Alfred.

Abstracts OS-specific behaviour so the rest of the codebase can import
from here rather than sprinkling ``sys.platform`` checks everywhere.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

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
            # macOS does not expose a simple swap-used figure; use None.
            swap_used_mib = None
        except (OSError, subprocess.SubprocessError, ValueError):
            return _NA

    try:
        count = os.cpu_count() or 1
        cpu_load = os.getloadavg()[0] / max(1, count)
    except (OSError, AttributeError):
        cpu_load = None  # Windows has no getloadavg

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
