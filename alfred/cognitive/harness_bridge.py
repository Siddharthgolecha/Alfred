"""Validated Alfred cognitive-contract projection; never a privileged tool wire.

These bounded structural frames are INTERNAL to Alfred, not the SDK's JSON-RPC
wire. The genuine SDK is driven by harness_sdk.LocalHarness. Untrusted harness
notifications are never promoted to authoritative tool observations.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import CONTRACT_TYPES, Contract, decode_contract

MAX_FRAME_BYTES = 8192
ALLOWED_EVENTS = frozenset(CONTRACT_TYPES)


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
    """Decode one strict v1 Alfred frame; tool proposals are not representable."""
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
    # Enforce EXACT canonical fields (including missing required ones), full
    # enum/type checks, and the same 2 KiB limit as the durable v1 ledger.
    cls = CONTRACT_TYPES[frame["event"]]
    try:
        canonical = {"kind": frame["event"], "version": 1, **payload}
        item = decode_contract(json.dumps(canonical, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False))
        if type(item) is not cls:
            raise HarnessProtocolError("Unexpected contract type")
    except (ValueError, TypeError, KeyError) as exc:
        raise HarnessProtocolError("Invalid canonical cognitive payload") from exc
    return frame


def encode_frame(event: str, payload: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps({"version": 1, "event": event, "payload": payload},
                         sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HarnessProtocolError("Invalid JSON frame") from exc
    decode_frame(raw)
    return raw


def frame_for_contract(contract: Contract) -> bytes:
    """Only ALFRED-authored typed ledger events may cross this boundary."""
    if not isinstance(contract, Contract):
        raise HarnessProtocolError("Expected canonical cognitive contract")
    # Pydantic v2 validates at construction time; no separate validate() call needed.
    kind = type(contract).model_fields["kind"].default
    return encode_frame(kind, contract.model_dump(exclude={"kind"}))


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
    """Metadata-only boundary; cannot authorize model-suggested tool calls."""

    def __init__(self, config: HarnessConfig = HarnessConfig()):
        config.validate()
        self.config = config

    def receive(self, raw: bytes) -> dict[str, Any]:
        if not self.config.enabled:
            raise HarnessProtocolError("harness disabled")
        return decode_frame(raw)

    def send(self, event: str, payload: dict[str, Any],
             transport: Callable[[bytes], bytes]) -> dict[str, Any]:
        """Fake or local-only structural transport; never executes proposals.

        This API cannot send executable tool arguments. Actual SDK JSON-RPC
        is handled by the official SDK, not this standalone frame format.
        """
        if not self.config.enabled:
            raise HarnessProtocolError("harness disabled")
        if not callable(transport):
            raise HarnessProtocolError("invalid transport")
        return self.receive(transport(encode_frame(event, payload)))
