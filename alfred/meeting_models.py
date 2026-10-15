"""Pydantic v2 models and pure validation helpers for offline meeting data.

Contains models and functions that are pure data concerns with no adapter
state: segment sanitisation, minutes validation, and the MeetingPermit model.
The OfflineMeetingAdapter class lives in alfred.meeting_adapter.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from .cognitive.contracts import valid_id
from .constants import Limits, Patterns

MEETING_ID = Patterns.MEETING_ID
SOURCE = Patterns.MEETING_SOURCE
OWNER = Patterns.MEETING_OWNER
_ID_PATTERN = Patterns.MEETING_ENTITY_ID
MAX_SEGMENTS = Limits.MAX_MEETING_SEGMENTS
MAX_EVIDENCE_BYTES = Limits.MAX_EVIDENCE_BYTES
SECRET = Patterns.MEETING_SECRET
CONTROLS = Patterns.MEETING_CONTROLS

UNKNOWN_OWNER = "Unassigned"
UNKNOWN_DATE = "Not specified"


# ---------------------------------------------------------------------------
# Internal text helpers
# ---------------------------------------------------------------------------

def _text(value: Any, limit: int) -> str:
    """Redact secrets and control characters, then truncate to *limit* chars."""
    if type(value) is not str:
        raise ValueError("Invalid evidence text")
    value = CONTROLS.sub(" ", value)
    value = SECRET.sub("[redacted]", value)
    return value[:limit]


def _clock_time(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp string with mandatory timezone."""
    if type(value) is not str or len(value) > Limits.MAX_TIMESTAMP_LEN:
        raise ValueError("Invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timezone is mandatory")
    return parsed.astimezone(timezone.utc)


def _canonical_utc(value: Any) -> str:
    """Return *value* as a canonical UTC ISO-8601 string."""
    return (
        _clock_time(value)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        .replace(".000000Z", "Z")
    )


# ---------------------------------------------------------------------------
# Segment sanitiser
# ---------------------------------------------------------------------------

def sanitize_segments(raw: Any) -> tuple[dict[str, Any], ...]:
    """Expose mic channels, overlaps, uncertainty and source timing, not identity.

    This is a bounded *current-meeting* packet only.  It does not read files or
    discover old transcripts.  Speaker labels are never verified person names.
    """
    if type(raw) not in (tuple, list) or not 0 < len(raw) <= MAX_SEGMENTS:
        raise ValueError("Expected 1..64 typed meeting segments")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for entry in raw:
        if type(entry) is not dict:
            raise ValueError("Malformed meeting segment")
        source = entry.get("source")
        if type(source) is not str or not SOURCE.fullmatch(source):
            raise ValueError("Invalid microphone channel identifier")
        start, end = entry.get("start_utc"), entry.get("end_utc")
        canonical_start = _canonical_utc(start)
        canonical_end = _canonical_utc(end)
        if _clock_time(end) < _clock_time(start):
            raise ValueError("Meeting segment timestamps reversed")
        sid = entry.get("id")
        if type(sid) is not str or not _ID_PATTERN.fullmatch(sid) or sid in ids:
            raise ValueError("Invalid/duplicate meeting segment ID")
        ids.add(sid)
        uncertainty = entry.get("synchronization_uncertainty_ms")
        if uncertainty is not None and (
            type(uncertainty) not in (float, int)
            or not math.isfinite(uncertainty)
            or not 0 <= uncertainty <= 30000
        ):
            raise ValueError("Invalid synchronization uncertainty")
        overlaps = entry.get("overlap_with", [])
        if (
            type(overlaps) is not list
            or len(overlaps) > 16
            or any(type(x) is not str or not _ID_PATTERN.fullmatch(x) for x in overlaps)
        ):
            raise ValueError("Invalid overlap links")
        for flag in ("likely_crosstalk", "ambiguous_crosstalk"):
            if type(entry.get(flag, False)) is not bool:
                raise ValueError("Invalid crosstalk flag")
        raw_words = entry.get("words", [])
        if type(raw_words) is not list or len(raw_words) > 48:
            raise ValueError("Unbounded word-timing evidence")
        words: list[dict[str, Any]] = []
        for word in raw_words:
            if type(word) is not dict:
                raise ValueError("Invalid timestamped word")
            a, b = word.get("start_seconds"), word.get("end_seconds")
            if (
                type(a) not in (float, int)
                or type(b) not in (float, int)
                or not math.isfinite(a)
                or not math.isfinite(b)
                or not 0 <= a <= b
            ):
                raise ValueError("Invalid word timestamp")
            word_text = _text(word.get("word"), 60)
            if not word_text:
                raise ValueError("Empty word")
            words.append({"word": word_text, "start_seconds": a, "end_seconds": b})
        result.append({
            "id": sid,
            "start_utc": canonical_start,
            "end_utc": canonical_end,
            "source": source,
            "speaker": source,
            "identity_type": "microphone_channel",
            "verified_identity": False,
            "text": _text(entry.get("text"), 260),
            "overlap_with": list(overlaps),
            "overlap": bool(overlaps),
            "likely_crosstalk": entry.get("likely_crosstalk", False),
            "ambiguous_crosstalk": entry.get("ambiguous_crosstalk", False),
            "synchronization_uncertainty_ms": uncertainty,
            "timing_basis": "decoder_estimated",
            "words": words,
        })
    for row in result:
        if any(x not in ids or x == row["id"] for x in row["overlap_with"]):
            raise ValueError("Dangling or self-referential overlap link")
    if len(json.dumps(result, ensure_ascii=True)) > MAX_EVIDENCE_BYTES:
        raise ValueError("Meeting evidence exceeds bounded local context")
    return tuple(result)


# ---------------------------------------------------------------------------
# Minutes validation
# ---------------------------------------------------------------------------

def _quote(raw: Any, by_id: dict[str, Any]) -> tuple[str, str]:
    """Only a short verbatim span from an explicit cited mic segment is admissible."""
    if (
        type(raw) is not dict
        or set(raw) != {"segment_id", "quote"}
        or type(raw["segment_id"]) is not str
        or raw["segment_id"] not in by_id
    ):
        raise ValueError("Uncited minutes claim")
    q = _text(raw["quote"], 300)
    source = by_id[raw["segment_id"]]["text"]
    if not 2 <= len(q) <= 300 or q.casefold() not in source.casefold():
        raise ValueError("Unsubstantiated minutes claim")
    return q, source


def validate_minutes_proposal(
    proposal: Any,
    segments: tuple[dict[str, Any], ...],
    *,
    meeting_title: str,
) -> dict[str, Any]:
    """Reject unsupported facts; no owner, date, identity or decision synthesis.

    A Jetson Qwen adapter should request citeable segment IDs + exact quotes.
    Device PDF writer receives *normalized* safe minutes, not model-chosen paths.
    """
    allowed = {"summary", "agenda", "key_points", "decisions", "action_items",
               "risks", "open_questions"}
    if type(proposal) is not dict or set(proposal) != allowed:
        raise ValueError("Invalid structured minutes proposal")
    title = _text(meeting_title, 100)
    if not title.strip():
        raise ValueError("Meeting title required")
    by_id = {item["id"]: item for item in segments}
    out: dict[str, Any] = {"title": title}
    for field in ("summary", "agenda", "key_points", "decisions", "risks", "open_questions"):
        values = proposal[field]
        if type(values) is not list or len(values) > 24:
            raise ValueError("Unbounded minutes section")
        verified = [_quote(value, by_id)[0] for value in values]
        out[field] = "\n".join(verified) if field == "summary" else verified
    if not out["summary"]:
        out["summary"] = "No evidence-backed summary available"
    actions = proposal["action_items"]
    if type(actions) is not list or len(actions) > 20:
        raise ValueError("Invalid action items")
    out["action_items"] = []
    for item in actions:
        if type(item) is not dict or set(item) != {"task", "owner", "due"}:
            raise ValueError("Invalid action item fields")
        task, _ = _quote(item["task"], by_id)
        owner = item["owner"]
        due = item["due"]
        if type(owner) is not str or len(owner) > 80 or (
            owner != UNKNOWN_OWNER
            and (not owner.strip() or owner.casefold() not in task.casefold())
        ):
            raise ValueError("Owner must be explicitly supported or unassigned")
        if type(due) is not str or (
            due != UNKNOWN_DATE
            and (
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)
                or due not in task
            )
        ):
            raise ValueError("Due date must be explicitly supported or unspecified")
        if owner in {seg["source"] for seg in segments}:
            raise ValueError("Microphone channel is not a verified person")
        out["action_items"].append({
            "task": task,
            "owner": owner,
            "due": due,
            "source_segment_id": item["task"]["segment_id"],
            "evidence_quote": task,
        })
    return out


# ---------------------------------------------------------------------------
# MeetingPermit — Pydantic v2 immutable model
# ---------------------------------------------------------------------------

class MeetingPermit(BaseModel):
    """Immutable per-task access grant for a single meeting."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    task_id: str
    owner: str
    meeting_id: str
    consent: bool
    allow_retention: bool
    granted_at: float
    expires_at: float

    @model_validator(mode="after")
    def _validate(self) -> "MeetingPermit":
        valid_id(self.session_id)
        valid_id(self.task_id)
        if not OWNER.fullmatch(self.owner):
            raise ValueError("Invalid meeting owner")
        if not MEETING_ID.fullmatch(self.meeting_id):
            raise ValueError("Invalid meeting ID")
        if not self.consent:
            raise PermissionError("Explicit consent required")
        if (
            not math.isfinite(self.granted_at)
            or not math.isfinite(self.expires_at)
            or not 0 < self.expires_at - self.granted_at <= 3600
        ):
            raise ValueError("Invalid meeting access duration")
        return self
