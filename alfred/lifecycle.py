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
import subprocess
import sys
import threading
import time

from .platform_utils import HAS_AF_UNIX, IS_WINDOWS

LOG = logging.getLogger(__name__)


def control_dir(logdir):
    """A private directory prevents another user from spoofing a close request."""
    path = Path(logdir).expanduser() / ".control"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    # Windows has no uid; skip the ownership/permission check there.
    if not IS_WINDOWS:
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError(
                f"Unsafe Alfred control directory: {path} (must be owned by you, mode 0700)"
            )
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

    def _cleanup_interrupted_bind(self, listener_socket: socket.socket, bound_here: bool) -> None:
        """Unlink the socket path if this descriptor successfully bound it before an interruption."""
        try:
            descriptor_bound = listener_socket.getsockname() == str(self.path)
        except OSError:
            descriptor_bound = False
        if descriptor_bound and not bound_here:
            # This is the narrow bind-interruption window. If a new
            # live listener replaced our unlinked pathname, the
            # descriptor still reports its old name; do NOT remove
            # the replacement. This socket has not called listen yet.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.path))
                except OSError:
                    pass
                else:
                    descriptor_bound = False
        if bound_here or descriptor_bound:
            try:
                info = self.path.lstat()
                uid_ok = IS_WINDOWS or info.st_uid == os.getuid()
                if (
                    stat.S_ISSOCK(info.st_mode)
                    and uid_ok
                    and (self._inode is None or info.st_ino == self._inode)
                ):
                    self.path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self):
        if not HAS_AF_UNIX:
            # Windows without AF_UNIX support: shutdown socket is unavailable;
            # behave as a no-op context manager.
            return self
        listener_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound_here = False
        try:
            if self.path.is_symlink() or self.path.exists():
                info = self.path.lstat()
                uid_mismatch = not IS_WINDOWS and info.st_uid != os.getuid()
                if not stat.S_ISSOCK(info.st_mode) or uid_mismatch:
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
            listener_socket.bind(str(self.path))
            bound_here = True
            os.chmod(self.path, 0o600)
            self._inode = self.path.stat().st_ino
            listener_socket.listen(4)
            listener_socket.settimeout(0.25)
            self._socket = listener_socket
            self._thread = threading.Thread(target=self._serve, name="alfred-close-listener", daemon=True)
            self._thread.start()
            return self
        except (OSError, RuntimeError, KeyboardInterrupt, SystemExit):
            # SIGINT/SIGTERM can interrupt __enter__ AFTER bind but BEFORE
            # the with statement installs __exit__. Unlink *only* the owned
            # socket we bound; otherwise a dead scheduler socket survives.
            self._stop.set()
            try:
                self._cleanup_interrupted_bind(listener_socket, bound_here)
            finally:
                listener_socket.close()
                if self._thread is not None and self._thread.is_alive():
                    self._thread.join(timeout=2)
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
        if not HAS_AF_UNIX:
            return  # No-op on platforms without AF_UNIX.
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        try:
            if self._inode is not None and self.path.lstat().st_ino == self._inode:
                self.path.unlink()
        except FileNotFoundError:
            pass


def request_close(logdir, role, wait_seconds=15):
    """Ask the live instance to stop; never send signals to a saved PID."""
    if not HAS_AF_UNIX:
        return False  # Shutdown socket not supported on this platform.
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


class ChildProcesses:
    """Track short-lived Alfred subprocesses so worker shutdown cannot orphan them.

    Only children spawned through this registry can be terminated. This does
    not affect deliberately started user services or externally owned servers.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._children = set()

    @staticmethod
    def _signal(process, sig):
        if process.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                # Windows has no process groups; terminate/kill the process directly.
                if sig == signal.SIGTERM:
                    process.terminate()
                else:
                    process.kill()
            else:
                try:
                    if os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, sig)
                    else:
                        process.send_signal(sig)
                except ProcessLookupError:
                    pass
        except OSError:
            pass

    @classmethod
    def _stop_child(cls, process, grace=2):
        if process.poll() is None:
            try:
                cls._signal(process, signal.SIGTERM)
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                cls._signal(process, signal.SIGKILL)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    LOG.warning("Unable to reap Alfred child PID %s", process.pid)
            except OSError as exc:
                LOG.warning("Unable to stop Alfred child PID %s: %s", process.pid, exc)

    def run(self, argv, *, timeout, capture_output=False, text=False, check=False):
        """A cancellable subset of subprocess.run for approved local commands."""
        popen_kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
        )
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("Alfred is shutting down; refusing new subprocess")
            process = subprocess.Popen(argv, **popen_kwargs)
            self._children.add(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._stop_child(process)
                raise
            done = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            if check:
                done.check_returncode()
            return done
        except BaseException:
            self._stop_child(process)
            raise
        finally:
            with self._lock:
                self._children.discard(process)

    def close(self):
        self._stopping.set()
        with self._lock:
            children = tuple(self._children)
        # Stop all children before waiting on any one of them.
        for process in children:
            try:
                self._signal(process, signal.SIGTERM)
            except OSError as exc:
                LOG.warning("Unable to signal Alfred child PID %s: %s", process.pid, exc)
        for process in children:
            self._stop_child(process, grace=2)
