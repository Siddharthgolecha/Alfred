"""Versioned, size-bounded, privacy-preserving cognitive contracts.

Only structural metadata is allowed. No prompts, transcripts, audio, tool
arguments, model responses or untrusted tool results are fields in these types.
"""
from __future__ import annotations

import json
import math
import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..constants import Limits, Patterns, Truncation

VERSION = 1
MAX_EVENT_BYTES = Truncation.CONTRACT_BYTES
ID_RE = Patterns.CORRELATION_ID
TOOL_RE = Patterns.TOOL_NAME


def new_id() -> str:
    """Return a new 32-character lowercase hex UUID."""
    return uuid.uuid4().hex


def valid_id(value: Any) -> str:
    """Validate and return *value* as a 32-char hex correlation ID."""
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("Invalid correlation ID")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook that rejects duplicate JSON keys at every depth."""
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


def _raise_nonfinite_json_error(_constant: str) -> Any:
    """parse_constant hook that rejects NaN/Infinity/-Infinity JSON constants."""
    raise ValueError("Nonfinite JSON")


class Contract(BaseModel):
    """Strict JSON codec rejecting unknown fields, versions and oversized values.

    Subclasses declare their typed fields directly.  The ``kind`` class
    variable identifies the contract type in serialised form.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Subclasses override this at class level (not as a field).
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a fully validated dict ready for JSON serialisation."""
        # Access kind via class model_fields to avoid Pydantic metaclass and
        # deprecation issues with instance-level model_fields access.
        kind_value = type(self).model_fields["kind"].default
        data = self.model_dump(exclude={"kind"})
        return {"kind": kind_value, "version": VERSION, **data}

    def to_json(self) -> str:
        """Serialise to compact UTF-8 JSON, enforcing the byte-size contract."""
        from ..validators import to_compact_json
        encoded = to_compact_json(self.to_dict())
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("Contract exceeds maximum size")
        return encoded

    @classmethod
    def from_json(cls, raw: str) -> "Contract":
        """Deserialise from a JSON string, rejecting any schema violations."""
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("Contract exceeds maximum size")
        try:
            obj = json.loads(raw, object_pairs_hook=_unique_pairs,
                             parse_constant=_raise_nonfinite_json_error)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed contract JSON") from exc
        if not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] != VERSION:
            raise ValueError("Unsupported contract version")
        # Access kind default via model_fields to avoid Pydantic metaclass issues.
        expected_kind = cls.model_fields["kind"].default
        if obj.get("kind") != expected_kind:
            raise ValueError("Incorrect contract kind")
        # Use model_fields (Pydantic v2) to get the declared field names.
        required = set(cls.model_fields) - {"kind"}
        if set(obj) - {"kind", "version"} != required:
            raise ValueError("Missing or unknown contract field")
        return cls(**{key: obj[key] for key in required})


def _reject_bool(v: Any) -> Any:
    """Pydantic v2 coerces bool→int; this before-validator stops that."""
    if type(v) is bool:
        raise ValueError("Boolean is not a valid integer value")
    return v


class TaskEnvelope(Contract):
    """A single authorised task boundary."""

    kind: str = "task"
    session_id: str
    task_id: str
    source: str
    input_chars: int
    started_at: int

    @field_validator("input_chars", "started_at", mode="before")
    @classmethod
    def _no_bool_ints(cls, v: Any) -> Any:
        return _reject_bool(v)

    @model_validator(mode="after")
    def _validate_fields(self) -> "TaskEnvelope":
        valid_id(self.session_id)
        valid_id(self.task_id)
        if self.source not in ("text", "voice", "reason"):
            raise ValueError("Unknown task source")
        if type(self.input_chars) is not int or not 0 <= self.input_chars <= Limits.TASK_MAX_INPUT_CHARS:
            raise ValueError("Invalid input length")
        if type(self.started_at) is not int or self.started_at < 0:
            raise ValueError("Invalid task timestamp")
        return self


class Decision(Contract):
    """A routing or planning decision made by one of the inference engines."""

    kind: str = "decision"
    engine: str
    route: str
    call_count: int
    confidence: Optional[float] = None

    @field_validator("call_count", mode="before")
    @classmethod
    def _no_bool_call_count(cls, v: Any) -> Any:
        return _reject_bool(v)

    @model_validator(mode="after")
    def _validate_fields(self) -> "Decision":
        if self.engine not in ("needle3", "qwen", "local"):
            raise ValueError("Unknown decision engine")
        if self.route not in ("direct", "delegate", "readonly", "uncertain",
                              "rejected", "shutdown"):
            raise ValueError("Unknown decision route")
        if type(self.call_count) is not int or not 0 <= self.call_count <= Limits.DECISION_MAX_CALL_COUNT:
            raise ValueError("Invalid call count")
        if self.confidence is not None:
            if type(self.confidence) not in (int, float) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
                raise ValueError("Invalid confidence")
        return self


class Observation(Contract):
    """The outcome of a single tool execution during a task."""

    kind: str = "observation"
    tool: str
    outcome: str

    @model_validator(mode="after")
    def _validate_fields(self) -> "Observation":
        if not isinstance(self.tool, str) or not TOOL_RE.fullmatch(self.tool):
            raise ValueError("Invalid tool name")
        if self.outcome not in ("returned", "blocked", "failed", "staged"):
            raise ValueError("Invalid tool outcome")
        return self


class Escalation(Contract):
    """A model-to-model handoff event."""

    kind: str = "escalation"
    source_model: str
    target_model: str
    reason: str

    @model_validator(mode="after")
    def _validate_fields(self) -> "Escalation":
        if self.source_model not in ("needle3", "local") or self.target_model != "qwen":
            raise ValueError("Invalid escalation")
        if self.reason not in ("unsupported", "delegated", "explicit_reason",
                               "uncertain", "incomplete"):
            raise ValueError("Invalid escalation reason")
        return self


class ApprovalReference(Contract):
    """A reference to a pending or completed local approval."""

    kind: str = "approval"
    approval_id: int

    @field_validator("approval_id", mode="before")
    @classmethod
    def _no_bool_approval_id(cls, v: Any) -> Any:
        return _reject_bool(v)

    @model_validator(mode="after")
    def _validate_fields(self) -> "ApprovalReference":
        if type(self.approval_id) is not int or not 1 <= self.approval_id <= Limits.MAX_APPROVAL_ID:
            raise ValueError("Invalid approval reference")
        return self


class Completion(Contract):
    """The final outcome of a task."""

    kind: str = "completion"
    outcome: str
    reason: str

    @model_validator(mode="after")
    def _validate_fields(self) -> "Completion":
        if self.outcome not in ("completed", "pending_approval", "rejected", "failed"):
            raise ValueError("Invalid completion outcome")
        if self.reason not in ("executed", "no_action", "needs_approval", "unsafe_route",
                               "low_confidence", "exception", "disabled", "shutdown",
                               "invalid_plan"):
            raise ValueError("Invalid completion reason")
        return self


class Diagnostic(Contract):
    """Opt-in diagnostic event. Whitelisted numeric/boolean fields only."""

    kind: str = "diagnostic"
    name: str
    metrics: dict[str, Any]

    @model_validator(mode="after")
    def _validate_fields(self) -> "Diagnostic":
        if self.name not in ("needle_route", "qwen_plan", "capture"):
            raise ValueError("Unknown diagnostic name")
        if not isinstance(self.metrics, dict) or len(self.metrics) > Limits.MAX_DIAGNOSTIC_METRICS:
            raise ValueError("Invalid diagnostic metrics")
        limits = {
            "latency_ms": Limits.DIAG_MAX_LATENCY_MS,
            "input_chars": Limits.DIAG_MAX_CHARS,
            "output_chars": Limits.DIAG_MAX_CHARS,
            "retry_count": Limits.DIAG_MAX_RETRY,
            "channels": Limits.DIAG_MAX_CHANNELS,
        }
        for key, value in self.metrics.items():
            if key not in limits or type(value) is not int or not 0 <= value <= limits[key]:
                raise ValueError("Diagnostic contains unsupported or sensitive data")
        return self


def _kind_of(cls: type[Contract]) -> str:
    """Return the ``kind`` default value for a Contract subclass."""
    return cls.model_fields["kind"].default  # type: ignore[return-value]


CONTRACT_TYPES: dict[str, type[Contract]] = {
    _kind_of(cls): cls
    for cls in (
        TaskEnvelope, Decision, Observation, Escalation,
        ApprovalReference, Completion, Diagnostic,
    )
}


def decode_contract(raw: str) -> Contract:
    """Dispatch *raw* JSON to the correct :class:`Contract` subclass."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("Invalid contract size")
    try:
        minimal = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed contract JSON") from exc
    if not isinstance(minimal, dict) or type(minimal.get("kind")) is not str:
        raise ValueError("Missing contract kind")
    cls = CONTRACT_TYPES.get(minimal["kind"])
    if cls is None:
        raise ValueError("Unknown contract kind")
    return cls.from_json(raw)
