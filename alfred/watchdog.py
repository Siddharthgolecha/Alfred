"""Model supervisor watchdog management for Alfred."""
from __future__ import annotations

import logging
import threading
from typing import Any

LOG = logging.getLogger(__name__)


class WatchdogManager:
    """Manages the background supervisor watchdog monitoring thread."""

    def __init__(
        self,
        supervisor: Any,
        admission: Any,
        poll_interval: float | int = 10,
        enabled: bool = True,
    ) -> None:
        self.supervisor = supervisor
        self.admission = admission
        self.poll_interval = int(poll_interval)
        self.enabled = enabled
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, poll_interval: float | int | None = None, enabled: bool | None = None) -> None:
        """Start the watchdog background thread if enabled."""
        if enabled is not None:
            self.enabled = enabled
        if poll_interval is not None:
            self.poll_interval = int(poll_interval)

        if not self.enabled:
            return
        if self.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="model-supervisor", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        """Monitor backend health without competing with foreground inference.

        Starting or loading a model is deliberately left to the request path,
        which already holds the shared inference admission slot. A periodic
        watchdog must not take that slot: doing so can make an actual user
        request fail while the watchdog is merely checking status.
        """
        while not self._stop_event.is_set():
            try:
                status = self.supervisor.status()
                if status.get("health") == "ready":
                    LOG.info("System2 available: %s", status)
                else:
                    LOG.info(
                        "System2 is not ready (health=%s); it will be started "
                        "by the next request",
                        status.get("health", "unknown"),
                    )
            except Exception as exc:
                LOG.warning("System2 health check failed; will retry: %s", exc)
            self._stop_event.wait(self.poll_interval)

    def stop(self) -> None:
        """Signal the watchdog thread to terminate."""
        self._stop_event.set()

    def join(self, timeout: float = 5.0) -> None:
        """Wait for the watchdog thread to finish."""
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
