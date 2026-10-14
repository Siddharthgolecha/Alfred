"""Fail-closed, offline-only boundary for an optional Cordis runtime.

This module deliberately does not grant Cordis any execution capability.
All tool proposals must be returned to Alfred's existing protected policy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_FRAME_BYTES = 8192
ALLOWED_EVENTS = frozenset({"task", "decision", "observation", "escalation", "completion"})


class HarnessProtocolError(ValueError):
    """Invalid or untrusted harness message."""


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise HarnessProtocolError("duplicate JSON key")
        obj[key] = value
    return obj


def decode_frame(raw: bytes) -> dict[str, Any]:
    """Decode bounded JSON; no tool call is executed here."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise HarnessProtocolError("invalid frame length")
    try:
        frame = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               HarnessProtocolError("nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessProtocolError("invalid frame encoding") from exc
    if not isinstance(frame, dict) or set(frame) != {"version", "event", "payload"}:
        raise HarnessProtocolError("invalid frame structure")
    if type(frame["version"]) is not int or frame["version"] != 1:
        raise HarnessProtocolError("unsupported protocol version")
    if frame["event"] not in ALLOWED_EVENTS or not isinstance(frame["payload"], dict):
        raise HarnessProtocolError("unsupported event")
    if any(key in frame["payload"] for key in ("execute", "shell", "plugin", "url", "approval")):
        raise HarnessProtocolError("execution or privilege escalation is forbidden")
    return frame


@dataclass(frozen=True)
class HarnessConfig:
    enabled: bool = False
    offline: bool = True
    allow_shell: bool = False
    allow_network: bool = False
    allow_plugins: bool = False

    def validate(self) -> None:
        if not all(type(v) is bool for v in vars(self).values()):
            raise HarnessProtocolError("configuration must be boolean")
        if not self.offline or self.allow_shell or self.allow_network or self.allow_plugins:
            raise HarnessProtocolError("unsafe harness capabilities requested")


class HarnessBridge:
    """Non-executing protocol boundary, ready for a separately pinned SDK adapter."""

    def __init__(self, config: HarnessConfig = HarnessConfig()):
        config.validate()
        self.config = config

    def receive(self, raw: bytes) -> dict[str, Any]:
        if not self.config.enabled:
            raise HarnessProtocolError("harness disabled")
        return decode_frame(raw)
