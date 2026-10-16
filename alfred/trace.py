"""Bounded, in-memory request traces for the opt-in debug interface."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import re
import threading
import time

MAX_REQUESTS = 100
MAX_EVENTS = 256


@dataclass
class TraceEvent:
    stage: str
    status: str
    detail: str = ""
    elapsed_ms: int | None = None


@dataclass
class RequestTrace:
    number: int
    source: str
    started: float
    events: list[TraceEvent] = field(default_factory=list)
    state: str = "running"
    dropped: int = 0


class TraceStore:
    """Collect structural events without transcripts, arguments, or tool data."""

    def __init__(self, *, enabled: bool = True, clock=time.monotonic):
        self.enabled = enabled
        self.clock = clock
        self.lock = threading.Lock()
        self.requests: deque[RequestTrace] = deque(maxlen=MAX_REQUESTS)
        self.current: RequestTrace | None = None
        self._next = 1

    def start(self, source: str) -> RequestTrace | None:
        if not self.enabled:
            return None
        with self.lock:
            request = RequestTrace(self._next, source, self.clock())
            self._next += 1
            self.requests.append(request)
            self.current = request
        self.record("Input", "complete", source.title())
        if source in ("text", "reason"):
            self.record("Speech recognition", "skipped", "Text input")
        if source == "reason":
            self.record("System1 · Needle", "skipped", "Explicit reasoning request")
            self.record("Context resolution", "skipped", "Not required")
        return request

    def record(self, stage: str, status: str, detail: str = "",
               elapsed_ms: int | None = None) -> None:
        if not self.enabled:
            return
        # Callers pass only fixed stage labels and allowlisted structural details.
        def literal(value, limit):
            return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))[:limit]

        event = TraceEvent(literal(stage, 64), literal(status, 24),
                           literal(detail, 160), elapsed_ms)
        with self.lock:
            if self.current is None:
                return
            if len(self.current.events) >= MAX_EVENTS:
                self.current.dropped += 1
                return
            self.current.events.append(event)

    def finish(self, state: str = "complete") -> None:
        if not self.enabled:
            return
        with self.lock:
            if self.current is not None:
                self.current.state = state[:24]
                self.current = None

    def snapshot(self):
        if not self.enabled:
            return [], None
        with self.lock:
            return list(self.requests), self.current.number if self.current else None


def elapsed_ms(start: float, clock=time.monotonic) -> int:
    return max(0, int((clock() - start) * 1000))
