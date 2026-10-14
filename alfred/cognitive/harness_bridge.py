"""Fail-closed optional harness boundary. No execution authority is delegated.

The official sdk-minimal profile is unsafe for Alfred (unrestricted shell).
Until a separately audited Cordis profile is pinned, this bridge only accepts
bounded, structural events and never starts a harness process.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

MAX_FRAME_BYTES = 8192
ALLOWED_EVENTS = frozenset({"task", "decision", "observation", "escalation", "completion"})
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class HarnessProtocolError(ValueError):
    """Invalid or untrusted harness message."""


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise HarnessProtocolError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(value):
    raise HarnessProtocolError("nonfinite JSON")


def _validate_value(value, depth=0):
    if depth > 8:
        raise HarnessProtocolError("nested payload too deep")
    if isinstance(value, str):
        if len(value) > 512:
            raise HarnessProtocolError("unbounded string")
    elif isinstance(value, list):
        if len(value) > 32:
            raise HarnessProtocolError("unbounded list")
        for item in value:
            _validate_value(item, depth + 1)
    elif isinstance(value, dict):
        if len(value) > 32:
            raise HarnessProtocolError("unbounded object")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 64:
                raise HarnessProtocolError("invalid key")
            _validate_value(item, depth + 1)
    elif value is None or type(value) in (bool, int):
        return
    elif type(value) is float and math.isfinite(value):
        return
    else:
        raise HarnessProtocolError("unsupported value")


def decode_frame(raw: bytes) -> dict[str, Any]:
    """Parse one bounded UTF-8 JSON envelope, never execute a proposal."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise HarnessProtocolError("invalid frame length")
    try:
        frame = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                           parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HarnessProtocolError("invalid frame encoding") from exc
    if not isinstance(frame, dict) or set(frame) != {"version", "event", "payload"}:
        raise HarnessProtocolError("invalid frame structure")
    if type(frame["version"]) is not int or frame["version"] != 1:
        raise HarnessProtocolError("unsupported protocol version")
    if type(frame["event"]) is not str or frame["event"] not in ALLOWED_EVENTS:
        raise HarnessProtocolError("unsupported event")
    payload = frame["payload"]
    if not isinstance(payload, dict):
        raise HarnessProtocolError("invalid event payload")
    _validate_value(payload)
    # Only structural metadata is accepted. Reject unknown fields, not just
    # dangerous known field names; this prevents aliases for shell/network.
    allowed = {
        "task": {"session_id", "task_id", "source", "input_chars"},
        "decision": {"engine", "route", "call_count", "confidence"},
        "observation": {"tool", "outcome"},
        "escalation": {"source_model", "target_model", "reason"},
        "completion": {"outcome", "reason"},
    }[frame["event"]]
    if not set(payload).issubset(allowed):
        raise HarnessProtocolError("unexpected payload field")
    if frame["event"] == "task":
        if not (ID_RE.fullmatch(str(payload.get("session_id", ""))) and
                ID_RE.fullmatch(str(payload.get("task_id", "")))):
            raise HarnessProtocolError("invalid task correlation")
        if payload.get("source") not in ("text", "voice", "reason"):
            raise HarnessProtocolError("invalid task source")
        n = payload.get("input_chars")
        if type(n) is not int or not 0 <= n <= 100000:
            raise HarnessProtocolError("invalid input size")
    elif frame["event"] == "observation":
        if not SAFE_TOOL_RE.fullmatch(str(payload.get("tool", ""))):
            raise HarnessProtocolError("invalid tool name")
        if payload.get("outcome") not in ("returned", "blocked", "failed", "staged"):
            raise HarnessProtocolError("invalid tool outcome")
    return frame


def encode_frame(event: str, payload: dict[str, Any]) -> bytes:
    raw = json.dumps({"version": 1, "event": event, "payload": payload},
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    decode_frame(raw)
    return raw


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
    """Validated event exchange, with no SDK launch or privileged tool execution."""

    def __init__(self, config: HarnessConfig = HarnessConfig()):
        config.validate()
        self.config = config

    def receive(self, raw: bytes) -> dict[str, Any]:
        if not self.config.enabled:
            raise HarnessProtocolError("harness disabled")
        return decode_frame(raw)

    def send(self, event: str, payload: dict[str, Any],
             transport: Callable[[bytes], bytes]) -> dict[str, Any]:
        """Exchange with an injected transport (fake tests only until SDK audit).

        A transport cannot gain execution authority through this method.
        """
        if not self.config.enabled:
            raise HarnessProtocolError("harness disabled")
        if not callable(transport):
            raise HarnessProtocolError("invalid transport")
        return self.receive(transport(encode_frame(event, payload)))
