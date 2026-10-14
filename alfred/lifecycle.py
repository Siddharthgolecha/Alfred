"""Per-user Alfred shutdown control. No PID file or shell-based process matching."""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import threading
import time

LOG = logging.getLogger(__name__)


def control_dir(logdir):
    """A private directory prevents another user from spoofing a close request."""
    path = Path(logdir).expanduser() / ".control"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"Unsafe Alfred control directory: {path} (must be owned by you, mode 0700)")
    return path


def _socket_path(logdir, role):
    if role not in ("voice", "scheduler"):
        raise ValueError("Unknown Alfred control role")
    path = control_dir(logdir) / (role + ".sock")
    if len(os.fsencode(path)) >= 104:
        raise OSError("Alfred control socket path is too long; shorten runtime.logs")
    return path


class ShutdownSocket:
    """One active listener per role, tied to the lifecycle of the application."""

    def __init__(self, logdir, role="voice"):
        self.path = _socket_path(logdir, role)
        self._socket = None
        self._thread = None
        self._stop = threading.Event()
        self._inode = None

    def __enter__(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if self.path.is_symlink() or self.path.exists():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise PermissionError(f"Unsafe Alfred control socket: {self.path}")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    try:
                        probe.settimeout(1)
                        probe.connect(str(self.path))
                    except OSError as exc:
                        if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                            raise
                    else:
                        raise RuntimeError(f"Alfred {self.path.stem} is already running")
                self.path.unlink()  # Stale socket; never touch an active listener.
            sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self._inode = self.path.stat().st_ino
            sock.listen(4)
            sock.settimeout(0.25)
            self._socket = sock
            self._thread = threading.Thread(target=self._serve, name="alfred-close-listener", daemon=True)
            self._thread.start()
            return self
        except BaseException:
            sock.close()
            raise

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2)
                    if hasattr(socket, "SO_PEERCRED"):
                        credentials = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        _, uid, _ = struct.unpack("3i", credentials)
                        if uid != os.getuid():
                            continue
                    command = conn.recv(32).strip()
                    if command == b"close":
                        conn.sendall(b"stopping\n")
                        # The main thread handles SIGTERM as KeyboardInterrupt
                        # and executes exactly the same finally cleanup as Ctrl+C.
                        os.kill(os.getpid(), signal.SIGTERM)
                    else:
                        conn.sendall(b"unknown command\n")
                except OSError as exc:
                    LOG.debug("Close request failed: %s", exc)

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        try:
            if self.path.lstat().st_ino == self._inode:
                self.path.unlink()
        except FileNotFoundError:
            pass


def request_close(logdir, role, wait_seconds=15):
    """Ask the live instance to stop; never send signals to a saved PID."""
    path = _socket_path(logdir, role)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        try:
            client.connect(str(path))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                return False
            raise
        if hasattr(socket, "SO_PEERCRED"):
            credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != os.getuid():
                raise PermissionError("Alfred listener belongs to a different user")
        client.sendall(b"close\n")
        reply = client.recv(32)
        if reply.strip() != b"stopping":
            raise RuntimeError(f"Alfred {role} refused close request: {reply!r}")
    deadline = time.monotonic() + wait_seconds
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if path.exists():
        raise TimeoutError(f"Alfred {role} did not finish shutting down within {wait_seconds}s")
    return True
