"""Keep native stdout/stderr chatter out of the conversational terminal."""
from __future__ import annotations

import logging
import os
import sys
import threading


class NativeOutputCapture:
    """Discard native output normally, or relay bounded lines to debug logging."""

    def __init__(self, *, debug=False):
        self.debug = debug
        self.saved = []
        self._readers = []
        self._write_fds = []
        self._null_fd = None
        self._streams = []

    def __enter__(self):
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
                self._streams.append((stream, getattr(stream, "line_buffering", None)))
            except (OSError, ValueError):
                pass
        try:
            for fd, name in ((1, "stdout"), (2, "stderr")):
                self.saved.append((fd, os.dup(fd)))
                if self.debug:
                    read_fd, write_fd = os.pipe()
                    self._write_fds.append(write_fd)
                    thread = threading.Thread(
                        target=self._drain, args=(read_fd, name), daemon=True,
                        name=f"alfred-native-{name}")
                    thread.start()
                    self._readers.append(thread)
                    os.dup2(write_fd, fd)
                else:
                    if self._null_fd is None:
                        self._null_fd = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(self._null_fd, fd)
            for stream, _previous in self._streams:
                try:
                    stream.reconfigure(line_buffering=True)
                except (AttributeError, OSError, ValueError):
                    pass
        except OSError:
            self.close()
        return self

    @staticmethod
    def _drain(read_fd, name):
        log = logging.getLogger("alfred.native")
        pending = bytearray()
        try:
            while True:
                chunk = os.read(read_fd, 2048)
                if not chunk:
                    break
                for byte in chunk:
                    if byte in (10, 13):
                        if pending:
                            log.debug("native %s: %s", name,
                                      bytes(pending).decode("utf-8", errors="replace"))
                            pending.clear()
                    elif len(pending) < 4096:
                        pending.append(byte)
                    else:
                        pending.extend(b"...")
                        log.debug("native %s: %s", name,
                                  bytes(pending).decode("utf-8", errors="replace"))
                        pending.clear()
            if pending:
                log.debug("native %s: %s", name,
                          bytes(pending).decode("utf-8", errors="replace"))
        except OSError:
            return
        finally:
            try:
                os.close(read_fd)
            except OSError:
                pass

    def close(self):
        for stream, _previous in self._streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for fd, saved_fd in reversed(self.saved):
            try:
                os.dup2(saved_fd, fd)
                os.close(saved_fd)
            except OSError:
                pass
        self.saved.clear()
        for stream, line_buffering in self._streams:
            if line_buffering is not None:
                try:
                    stream.reconfigure(line_buffering=line_buffering)
                except (AttributeError, OSError, ValueError):
                    pass
        self._streams.clear()
        for fd in self._write_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._write_fds.clear()
        if self._null_fd is not None:
            try:
                os.close(self._null_fd)
            except OSError:
                pass
            self._null_fd = None
        for thread in self._readers:
            thread.join(timeout=1)
        self._readers.clear()

    def __exit__(self, *_exc):
        self.close()
