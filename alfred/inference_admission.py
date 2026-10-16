"""Opt-in Jetson inference admission; no model, tool or network privileges.

Single serialized expensive-inference slot shared by Needle, Whistle, Qwen,
and optional Harness. This is a *thread-level* resource gate, not an OS GPU
scheduler; native calls cannot be preempted after admission. No queued or
failed call is retried by the gate, including model proposals with side effects.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import threading
import time

from .platform_utils import IS_MACOS, sample_resources as _platform_sample_resources


@dataclass(frozen=True)
class AdmissionConfig:
    """Resource admission thresholds and limits for inference scheduling."""

    enabled: bool = False
    min_available_mib: int = 512
    max_swap_used_mib: int = 1024
    max_cpu_load_per_core: float = 3.0
    max_wait_ms: int = 200

    def __post_init__(self):
        if (
            type(self.enabled) is not bool
            or type(self.min_available_mib) is not int
            or not 128 <= self.min_available_mib <= 4096
            or type(self.max_swap_used_mib) is not int
            or not 0 <= self.max_swap_used_mib <= 8192
            or type(self.max_wait_ms) is not int
            or not 0 <= self.max_wait_ms <= 10000
            or type(self.max_cpu_load_per_core) not in (int, float)
            or not math.isfinite(self.max_cpu_load_per_core)
            or not 0.1 <= self.max_cpu_load_per_core <= 20
        ):
            raise ValueError("Invalid admission thresholds")


class AdmissionRejected(RuntimeError):
    """No model inference was admitted; no tools were executed by this gate."""


def linux_resources():
    """Best-effort local sample; never run tegrastats subprocess in audio path.

    On non-Linux platforms this falls back to the cross-platform sampler in
    :mod:`alfred.platform_utils`.  Kept for backward-compat (tests patch this).
    """
    return _platform_sample_resources()


class InferenceAdmission:
    """FIFO single-slot gate with voice priority, bounded wait and shutdown.

    `capture_active` inhibits new optional/expensive Qwen/Harness requests so
    capture + Whistle have priority *before* inference begins. It does not
    preempt an already running native model; hardware benchmarks must assess
    this limitation. Scheduler/tool execution must NEVER acquire this gate.
    """

    KINDS=frozenset(("needle","needle_feedback","whistle","qwen",
                     "qwen_explain","harness"))

    # Two voice waiters allow immediate buffering of consecutive wake phrases
    # while STT processes; one other waiter prevents background/batch inference
    # from piling up and exhausting Jetson unified memory.
    MAX_VOICE_WAITERS = 2
    MAX_OTHER_WAITERS = 1

    def __init__(self, config: AdmissionConfig | None = None, *,
                 enabled=False, min_available_mib=512,
                 max_swap_used_mib=1024, max_cpu_load_per_core=3.0,
                 max_wait_ms=200, sampler=_platform_sample_resources,
                 clock=time.monotonic):
        if config is not None:
            if not isinstance(config, AdmissionConfig):
                raise ValueError("Expected AdmissionConfig instance")
            self.config = config
        else:
            self.config = AdmissionConfig(
                enabled=enabled,
                min_available_mib=min_available_mib,
                max_swap_used_mib=max_swap_used_mib,
                max_cpu_load_per_core=max_cpu_load_per_core,
                max_wait_ms=max_wait_ms,
            )
        self.enabled = self.config.enabled
        self.min_available_mib = self.config.min_available_mib
        self.max_swap_used_mib = self.config.max_swap_used_mib
        self.max_cpu_load_per_core = self.config.max_cpu_load_per_core
        self.max_wait_ms = self.config.max_wait_ms
        self.sampler=sampler
        self.clock=clock
        self._cond=threading.Condition(threading.RLock())
        self._owner=None
        self._queue=[]
        self._capture=False
        self._closed=False
        self._counts={"granted":0,"rejected":0,"pressure":0,"queued":0}
        self._latencies=[]
        self._sampled=None
        self._pressure_reasons=[]

    def set_capture_active(self, active):
        if type(active) is not bool:
            raise ValueError("Capture state must be boolean")
        with self._cond:
            self._capture=active
            self._cond.notify_all()

    @property
    def capture_active(self):
        with self._cond:
            return self._capture

    def _pressure(self, kind):
        sample=self.sampler()
        if not isinstance(sample,dict):
            raise AdmissionRejected("Resource sampler unavailable")
        vals=(sample.get("available_mib"),sample.get("swap_used_mib"),
              sample.get("cpu_load_per_core"))
        if any(type(x) not in (float,int) or not math.isfinite(x)
               or x<0 for x in vals):
            # In opt-in mode, unknown device resource pressure refuses new
            # native inference; no invented safe memory headroom.
            raise AdmissionRejected("Unmeasured resource headroom")
        self._sampled={k:round(float(v),3) for k,v in zip(
            ("available_mib","swap_used_mib","cpu_load_per_core"),vals)}
        # Whistle during active voice capture takes priority over optional
        # model turns, but still refuses truly critical memory exhaustion.
        reasons=[]
        if vals[0]<self.min_available_mib:
            reasons.append(f"available memory {vals[0]:.0f} MiB is below "
                           f"{self.min_available_mib} MiB")
        # macOS retains swapped-out pages after pressure has passed. Current
        # available memory is a better admission signal there; swap remains
        # recorded for diagnostics, but swap use alone must not block work.
        if not IS_MACOS and vals[1]>self.max_swap_used_mib:
            reasons.append(f"swap use {vals[1]:.0f} MiB exceeds "
                           f"{self.max_swap_used_mib} MiB")
        if kind!="whistle" and vals[2]>self.max_cpu_load_per_core:
            reasons.append(f"CPU load per core {vals[2]:.2f} exceeds "
                           f"{self.max_cpu_load_per_core:.2f}")
        self._pressure_reasons=reasons
        return bool(reasons)

    def _queue_is_full(self, kind):
        # Fixed per-priority admission slots. A burst of simultaneous
        # Whistle callers must not accumulate unbounded Python threads.
        # The single background slot is kept separate from two reserved
        # voice waiters. Every accepted priority class remains FIFO.
        # Caller must hold self._cond.
        voice=sum(k=="whistle" for _,k in self._queue)
        other=len(self._queue)-voice
        return ((kind=="whistle" and voice>=self.MAX_VOICE_WAITERS)
                or (kind!="whistle" and other>=self.MAX_OTHER_WAITERS))

    def _is_my_turn(self, ticket, kind):
        # Whistle wins the next free slot over queued background
        # turns, but never overtakes an earlier Whistle ticket.
        # Bounded deadlines prevent indefinite background waits.
        # Caller must hold self._cond.
        first_voice=next((t for t,k in self._queue
                          if k=="whistle"),None)
        first_other=next((t for t,k in self._queue
                          if k!="whistle"),None)
        return (self._owner is None and (
            first_voice is ticket if kind=="whistle"
            else (not self._capture and first_voice is None
                  and first_other is ticket)))

    @contextmanager
    def claim(self, kind):
        if kind not in self.KINDS:
            raise ValueError("Unknown inference type")
        if not self.enabled:
            yield
            return
        ticket=object()
        start=self.clock()
        deadline=start+self.max_wait_ms/1000.
        with self._cond:
            if self._closed:
                raise AdmissionRejected("Inference admission shut down")
            if self._queue_is_full(kind):
                self._counts["rejected"]+=1
                raise AdmissionRejected("Inference priority queue full")
            self._queue.append((ticket,kind))
            if len(self._queue)>1:
                self._counts["queued"]+=1
            acquired=False
            try:
                while True:
                    if self._closed:
                        raise AdmissionRejected("Inference admission shut down")
                    if self._is_my_turn(ticket,kind):
                        try:
                            pressure=self._pressure(kind)
                        except AdmissionRejected:
                            self._counts["pressure"]+=1
                            raise
                        if pressure:
                            self._counts["pressure"]+=1
                            raise AdmissionRejected("Inference refused: "
                                                    + "; ".join(self._pressure_reasons))
                        self._owner=ticket
                        self._counts["granted"]+=1
                        self._latencies.append(min(10000.,max(0.,(self.clock()-start)*1000.)))
                        if len(self._latencies)>128:
                            self._latencies.pop(0)
                        acquired=True
                        break
                    remaining_seconds = deadline - self.clock()
                    if remaining_seconds <= 0:
                        self._counts["rejected"] += 1
                        raise AdmissionRejected("Inference queue busy; explicit retry required")
                    self._cond.wait(timeout=remaining_seconds)
            finally:
                # A rejected ticket must NEVER block subsequent work.
                self._queue=[q for q in self._queue if q[0] is not ticket]
                self._cond.notify_all()
        try:
            yield
        finally:
            with self._cond:
                if acquired and self._owner is ticket:
                    self._owner=None
                self._cond.notify_all()

    def allow_optional_diagnostics(self):
        """Drop optional telemetry rather than steal memory from Whistle/QA."""
        if not self.enabled:
            return True
        with self._cond:
            if self._capture or self._owner is not None or self._queue:
                return False
            try:
                return not self._pressure("qwen")
            except AdmissionRejected:
                return False

    def snapshot(self):
        """Only bounded numeric/boolean metadata; no transcript/model payloads."""
        with self._cond:
            return {"enabled":self.enabled,"capture_active":self._capture,
                    "busy":self._owner is not None,"queue_depth":len(self._queue),
                    **self._counts, "sample":dict(self._sampled or {})}

    def close(self):
        with self._cond:
            self._closed=True
            self._capture=False
            self._cond.notify_all()
