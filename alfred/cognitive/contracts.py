"""Versioned, size-bounded, privacy-preserving cognitive contracts.

Only structural metadata is allowed. No prompts, transcripts, audio, tool
arguments, model responses or untrusted tool results are fields in these types.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
import math
import re
import uuid

VERSION = 1
MAX_EVENT_BYTES = 2048
ID_RE = re.compile(r"^[0-9a-f]{32}$")
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def new_id() -> str:
    return uuid.uuid4().hex


def valid_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("Invalid correlation ID")
    return value


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


class Contract:
    """Strict JSON codec rejecting unknown fields, versions and oversized values."""
    kind = ""

    def validate(self):
        raise NotImplementedError

    def to_dict(self):
        self.validate()
        return {"kind": self.kind, "version": VERSION, **asdict(self)}

    def to_json(self):
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("Contract exceeds maximum size")
        return encoded

    @classmethod
    def from_json(cls, raw):
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("Contract exceeds maximum size")
        try:
            obj = json.loads(raw, object_pairs_hook=_unique_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed contract JSON") from exc
        if not isinstance(obj, dict) or type(obj.get("version")) is not int or obj["version"] != VERSION:
            raise ValueError("Unsupported contract version")
        if obj.get("kind") != cls.kind:
            raise ValueError("Incorrect contract kind")
        required = {f.name for f in fields(cls)}
        if set(obj) != required | {"kind", "version"}:
            raise ValueError("Missing or unknown contract field")
        instance = cls(**{key: obj[key] for key in required})
        instance.validate()
        return instance


@dataclass(frozen=True)
class TaskEnvelope(Contract):
    kind = "task"
    session_id: str
    task_id: str
    source: str
    input_chars: int
    started_at: int

    def validate(self):
        valid_id(self.session_id)
        valid_id(self.task_id)
        if self.source not in ("text", "voice", "reason"):
            raise ValueError("Unknown task source")
        if type(self.input_chars) is not int or not 0 <= self.input_chars <= 100000:
            raise ValueError("Invalid input length")
        if type(self.started_at) is not int or self.started_at < 0:
            raise ValueError("Invalid task timestamp")


@dataclass(frozen=True)
class Decision(Contract):
    kind = "decision"
    engine: str
    route: str
    call_count: int
    confidence: float | None = None

    def validate(self):
        if self.engine not in ("needle3", "qwen", "local"):
            raise ValueError("Unknown decision engine")
        if self.route not in ("direct", "delegate", "readonly", "uncertain",
                              "rejected", "shutdown"):
            raise ValueError("Unknown decision route")
        if type(self.call_count) is not int or not 0 <= self.call_count <= 50:
            raise ValueError("Invalid call count")
        if self.confidence is not None:
            if type(self.confidence) not in (int, float) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
                raise ValueError("Invalid confidence")


@dataclass(frozen=True)
class Observation(Contract):
    kind = "observation"
    tool: str
    outcome: str

    def validate(self):
        if not isinstance(self.tool, str) or not TOOL_RE.fullmatch(self.tool):
            raise ValueError("Invalid tool name")
        if self.outcome not in ("returned", "blocked", "failed", "staged"):
            raise ValueError("Invalid tool outcome")


@dataclass(frozen=True)
class Escalation(Contract):
    kind = "escalation"
    source_model: str
    target_model: str
    reason: str

    def validate(self):
        if self.source_model not in ("needle3", "local") or self.target_model != "qwen":
            raise ValueError("Invalid escalation")
        if self.reason not in ("unsupported", "delegated", "explicit_reason",
                             "uncertain", "incomplete"):
            raise ValueError("Invalid escalation reason")


@dataclass(frozen=True)
class ApprovalReference(Contract):
    kind = "approval"
    approval_id: int

    def validate(self):
        if type(self.approval_id) is not int or not 1 <= self.approval_id <= 2147483647:
            raise ValueError("Invalid approval reference")


@dataclass(frozen=True)
class Completion(Contract):
    kind = "completion"
    outcome: str
    reason: str

    def validate(self):
        if self.outcome not in ("completed", "pending_approval", "rejected", "failed"):
            raise ValueError("Invalid completion outcome")
        if self.reason not in ("executed", "no_action", "needs_approval", "unsafe_route",
                               "low_confidence", "exception", "disabled", "shutdown"):
            raise ValueError("Invalid completion reason")



@dataclass(frozen=True)
class Diagnostic(Contract):
    """Opt-in diagnostic. Whitelisted numeric/boolean fields remove all free text."""
    kind = "diagnostic"
    name: str
    metrics: dict

    def validate(self):
        if self.name not in ("needle_route", "qwen_plan", "capture"):
            raise ValueError("Unknown diagnostic name")
        if not isinstance(self.metrics, dict) or len(self.metrics) > 5:
            raise ValueError("Invalid diagnostic metrics")
        limits = {"latency_ms": 300000, "input_chars": 100000,
                  "output_chars": 100000, "retry_count": 100,
                  "channels": 32}
        for key, value in self.metrics.items():
            if key not in limits or type(value) is not int or not 0 <= value <= limits[key]:
                raise ValueError("Diagnostic contains unsupported or sensitive data")


CONTRACT_TYPES = {cls.kind: cls for cls in
                  (TaskEnvelope, Decision, Observation, Escalation, ApprovalReference, Completion, Diagnostic)}


def decode_contract(raw):
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
