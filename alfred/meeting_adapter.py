"""Offline meeting adapter — OfflineMeetingAdapter class only.

Data models, segment sanitisation and minutes validation live in
alfred.meeting_models and are re-exported here for backward compatibility.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any

from .cognitive.contracts import valid_id
from .constants import Limits, Patterns
from .meeting_models import (  # re-exported for existing callers
    MeetingPermit,
    UNKNOWN_OWNER,
    UNKNOWN_DATE,
    sanitize_segments,
    validate_minutes_proposal,
)

MEETING_ID = Patterns.MEETING_ID
OWNER = Patterns.MEETING_OWNER
ARTIFACT = Patterns.MEETING_ARTIFACT
MAX_SEGMENTS = Limits.MAX_MEETING_SEGMENTS
MAX_EVIDENCE_BYTES = Limits.MAX_EVIDENCE_BYTES

__all__ = [
    "OfflineMeetingAdapter",
    "MeetingPermit",
    "sanitize_segments",
    "validate_minutes_proposal",
    "UNKNOWN_OWNER",
    "UNKNOWN_DATE",
]


class OfflineMeetingAdapter:
    """Optional provider boundary with per-task access and no auto-install.

    Providers are injected by trusted host integration (e.g. Jetson adapter
    compatibility PR). No global transcript enumeration or path-based reads.
    """

    OPERATIONS: frozenset[str] = frozenset((
        "meeting_recording", "transcript_retrieval",
        "qwen_minutes", "qr_obex_handoff",
    ))

    def __init__(self, provider: Any = None, *, journal: Any = None,
                 clock: Any = time.time) -> None:
        self._provider = provider
        self._journal = journal
        self._clock = clock
        self._lock = threading.RLock()
        self._meetings: dict[str, dict[str, Any]] = {}      # meeting_id → trusted session/owner/retention
        self._permits: dict[tuple[str, str], MeetingPermit] = {}  # (task_id, meeting_id) → permit
        self._artifacts: dict[tuple[str, str], str] = {}    # (meeting_id, artifact_id) → task_id
        self._validated_minutes: dict[tuple[str, str], tuple[str, str]] = {}  # (task_id, meeting_id) → digest

    def available(self, skill: str) -> bool:
        """Return True only when the injected provider attests all required primitives."""
        if skill not in self.OPERATIONS:
            raise ValueError("Unknown meeting capability")
        if self._provider is None:
            return False
        probe = getattr(self._provider, "capability", None)
        if not callable(probe):
            return False
        try:
            required = {
                "meeting_recording": ("microphone", "guarded_ssd", "recording"),
                "transcript_retrieval": ("transcription", "guarded_ssd"),
                "qwen_minutes": ("transcription", "qwen", "safe_pdf"),
                "qr_obex_handoff": ("safe_pdf", "qr_obex"),
            }[skill]
            return all(probe(x) is True for x in required)
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def start(self, *, session_id: str, task_id: str, owner: str, consent: bool,
              retention: bool = False, local_authorized: bool = False) -> dict[str, Any]:
        """Only an authorized operator/wake task can activate microphones.

        Device-specific adapter chooses its own guarded storage and meeting ID.
        Model-generated text is not an authorization token.
        """
        if not local_authorized or consent is not True or type(retention) is not bool:
            raise PermissionError("Meeting start requires trusted local authorization and consent")
        valid_id(session_id)
        valid_id(task_id)
        if type(owner) is not str or not OWNER.fullmatch(owner):
            raise ValueError("Invalid owner")
        if not self.available("meeting_recording"):
            raise RuntimeError("Offline meeting recording unavailable")
        if self._journal is None or not callable(getattr(
                self._provider, "rollback_start", None)):
            raise RuntimeError("Recording requires durable journal and token rollback")
        with self._lock:
            operation_id = secrets.token_hex(16)
            self._journal.put(operation_id, session_id, task_id, owner,
                              retained=retention, state="starting")
            try:
                meeting_id = self._provider.start_recording(
                    consent=True, retention=retention, operation_id=operation_id)
                if type(meeting_id) is not str or not MEETING_ID.fullmatch(meeting_id):
                    raise ValueError("Device returned an invalid meeting ID")
                if meeting_id in self._meetings:
                    raise ValueError("Duplicate meeting ID")
                self._journal.put(operation_id, session_id, task_id, owner,
                                  meeting_id=meeting_id, retained=retention,
                                  state="recording")
                self._meetings[meeting_id] = {
                    "session_id": session_id, "owner": owner,
                    "retention": retention, "active": True,
                    "operation_id": operation_id,
                }
            except BaseException:
                # Never assume a failing provider didn't start the mic.
                self._journal.put(operation_id, session_id, task_id, owner,
                                  retained=retention, state="rollback_pending")
                try:
                    rolled_back = self._provider.rollback_start(operation_id) is True
                except (OSError, RuntimeError, ValueError):
                    rolled_back = False
                if rolled_back:
                    self._journal.remove(operation_id)
                    raise
                raise RuntimeError(
                    "Recorder registration failed; rollback unverified; "
                    "authorized durable cleanup required")
        return {"meeting_id": meeting_id, "status": "recording",
                "identity": "mic_channel_not_person", "retention": retention}

    def permit(self, *, session_id: str, task_id: str, owner: str, meeting_id: str,
               consent: bool, local_authorized: bool = False,
               ttl_seconds: int = 600) -> MeetingPermit:
        """Issue a per-task immutable access permit for *meeting_id*."""
        if not local_authorized or consent is not True:
            raise PermissionError("Each task requires explicit meeting access consent")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError("Invalid consent TTL")
        valid_id(session_id)
        valid_id(task_id)
        with self._lock:
            meeting = self._meetings.get(meeting_id)
            if (meeting is None or meeting["session_id"] != session_id
                    or meeting["owner"] != owner):
                raise PermissionError("Meeting is outside authorized session/owner")
            if not meeting["active"] and not meeting["retention"]:
                raise PermissionError("Nonretained meeting cannot be reopened")
            now = float(self._clock())
            p = MeetingPermit(session_id=session_id, task_id=task_id, owner=owner,
                              meeting_id=meeting_id, consent=True,
                              allow_retention=meeting["retention"],
                              granted_at=now, expires_at=now + ttl_seconds)
            self._permits[(task_id, meeting_id)] = p
            return p

    @staticmethod
    def _require_meeting_structure(meeting: Any, permit: "MeetingPermit") -> None:
        """Validate that *meeting* has the expected structure and matches *permit*."""
        if (
            not isinstance(meeting, dict)
            or meeting.get("owner") != permit.owner
            or meeting.get("session_id") != permit.session_id
        ):
            raise PermissionError("Meeting record is missing or does not match permit")

    def _verify(self, permit: Any, *, active: bool = False, skill: str | None = None) -> None:
        """Assert that *permit* is a live, non-expired permit for the current meeting."""
        if type(permit) is not MeetingPermit:
            raise PermissionError("Trusted permit required")
        with self._lock:
            if self._permits.get((permit.task_id, permit.meeting_id)) is not permit:
                raise PermissionError("Meeting permit revoked or fabricated")
            meeting = self._meetings.get(permit.meeting_id)
            self._require_meeting_structure(meeting, permit)
            if (
                meeting.get("state") in ("stopping", "purge_pending")
                or (not meeting["active"] and not meeting["retention"])
                or float(self._clock()) >= permit.expires_at
            ):
                raise PermissionError("Meeting permit expired or outside scope")
            if active and not meeting["active"]:
                raise PermissionError("Meeting is not active")
            if skill and not self.available(skill):
                raise RuntimeError("Device capability unavailable")

    def status(self, permit: MeetingPermit) -> dict[str, Any]:
        """Return current recording state from the device provider."""
        with self._lock:
            self._verify(permit, active=True, skill="meeting_recording")
            response = self._provider.recording_status(permit.meeting_id)
            if type(response) is not dict or response.get("state") not in ("recording", "stopping"):
                raise ValueError("Device returned invalid meeting status")
            return {"meeting_id": permit.meeting_id, "state": response["state"],
                    "identity": "mic_channel_not_person"}

    def stop(self, permit: MeetingPermit, *, local_authorized: bool = False) -> dict[str, Any]:
        """Stop an active recording; non-retained evidence is purged immediately."""
        if local_authorized is not True:
            raise PermissionError("Stopping a recording requires separate local authorization")
        with self._lock:
            self._verify(permit, active=True, skill="meeting_recording")
            if self._journal is None:
                raise RuntimeError("Recording cleanup journal unavailable")
            meeting = self._meetings[permit.meeting_id]
            operation_id = meeting["operation_id"]
            retained = meeting["retention"]
            self._journal.put(operation_id, permit.session_id, permit.task_id,
                              permit.owner, meeting_id=permit.meeting_id,
                              retained=retained, state="stopping")
            meeting["active"] = False
            meeting["state"] = "stopping"
            if not retained:
                self._revoke_meeting_access(permit.meeting_id)
            self._provider.stop_recording(permit.meeting_id)
            if retained:
                self._journal.put(
                    operation_id, permit.session_id, permit.task_id,
                    permit.owner, meeting_id=permit.meeting_id,
                    retained=True, state="stopped_retained")
                meeting["state"] = "stopped"
                return {"meeting_id": permit.meeting_id, "state": "stopped", "retained": True}
            self._journal.put(operation_id, permit.session_id, permit.task_id,
                              permit.owner, meeting_id=permit.meeting_id,
                              retained=False, state="purge_pending")
            meeting["state"] = "purge_pending"
            self._discard_or_raise(permit.meeting_id)
            self._journal.remove(operation_id)
            meeting["state"] = "purged"
            return {"meeting_id": permit.meeting_id, "state": "stopped", "retained": False}

    def _revoke_permits(self, meeting_id: str) -> None:
        """Remove all task permits and their validated minutes for *meeting_id*."""
        for key in tuple(self._permits):
            if key[1] == meeting_id:
                del self._permits[key]
                self._validated_minutes.pop(key, None)

    def _delete_minutes(self, meeting_id: str) -> None:
        """Remove all cached validated minutes entries for *meeting_id*."""
        for key in tuple(self._validated_minutes):
            if key[1] == meeting_id:
                self._validated_minutes.pop(key, None)

    def _delete_artifacts(self, meeting_id: str) -> None:
        """Remove all artifact references for *meeting_id*."""
        for key in tuple(self._artifacts):
            if key[0] == meeting_id:
                del self._artifacts[key]

    def _revoke_meeting_access(self, meeting_id: str) -> None:
        """Remove all permits, validated minutes, and artifacts for *meeting_id*."""
        self._revoke_permits(meeting_id)
        self._delete_artifacts(meeting_id)

    def _discard_or_raise(self, meeting_id: str) -> None:
        """Call provider discard and raise if it cannot be verified."""
        discard = getattr(self._provider, "discard_meeting", None)
        try:
            verified = callable(discard) and discard(meeting_id) is True
        except (OSError, RuntimeError, ValueError):
            verified = False
        if not verified:
            raise RuntimeError(
                "Recorder stopped, but secure meeting discard unverified; "
                "durable cleanup pending and authorized retry required")

    def recover_cleanup(self, *, session_id: str, owner: str,
                        local_authorized: bool = False) -> list[dict[str, Any]]:
        """Trusted local operator-only recovery, including AFTER process restart.

        No historical transcript read is permitted. The journal contains
        opaque operation identifiers only. Token rollback and stop/discard
        MUST be idempotent in the injected device provider. Unverified
        cleanup stays durable and raises, never claims a successful erase.
        """
        if not local_authorized:
            raise PermissionError("Cleanup recovery requires local authorization")
        valid_id(session_id)
        if type(owner) is not str or not OWNER.fullmatch(owner):
            raise ValueError("Invalid cleanup scope")
        if self._journal is None:
            raise RuntimeError("No durable cleanup journal")
        completed: list[dict[str, Any]] = []
        with self._lock:
            for row in self._journal.pending(session_id, owner):
                op = row["operation_id"]
                mid = row["meeting_id"]
                state = row["state"]
                if state in ("starting", "rollback_pending"):
                    rollback = getattr(self._provider, "rollback_start", None)
                    try:
                        ok = callable(rollback) and rollback(op) is True
                    except (OSError, RuntimeError, ValueError):
                        ok = False
                    if not ok:
                        raise RuntimeError("Recorder startup rollback still unverified")
                    self._journal.remove(op)
                    completed.append({"operation_id": op, "state": "rolled_back"})
                    continue
                if not isinstance(mid, str) or not MEETING_ID.fullmatch(mid):
                    raise RuntimeError("Cleanup journal lacks valid meeting ID")
                if state in ("recording", "stopping"):
                    self._journal.put(op, row["session_id"], row["task_id"],
                                      owner, meeting_id=mid,
                                      retained=bool(row["retained"]), state="stopping")
                    self._provider.stop_recording(mid)
                    if row["retained"]:
                        self._journal.put(
                            op, row["session_id"], row["task_id"], owner,
                            meeting_id=mid, retained=True, state="stopped_retained")
                        completed.append({"meeting_id": mid, "state": "stopped_retained"})
                        if mid in self._meetings:
                            self._meetings[mid]["active"] = False
                            self._meetings[mid]["state"] = "stopped"
                            self._revoke_meeting_access(mid)
                        continue
                    self._journal.put(op, row["session_id"], row["task_id"],
                                      owner, meeting_id=mid,
                                      retained=False, state="purge_pending")
                if mid in self._meetings:
                    self._meetings[mid]["active"] = False
                    self._meetings[mid]["state"] = "purge_pending"
                self._revoke_meeting_access(mid)
                self._discard_or_raise(mid)
                self._journal.remove(op)
                if mid in self._meetings:
                    self._meetings[mid]["state"] = "purged"
                completed.append({"meeting_id": mid, "state": "purged"})
        return completed

    def evidence(self, permit: MeetingPermit) -> tuple[dict[str, Any], ...]:
        """Return sanitised segments for *permit*; never lists historical runs."""
        with self._lock:
            self._verify(permit, skill="transcript_retrieval")
            return sanitize_segments(self._provider.get_segments(permit.meeting_id))

    def minutes(self, permit: MeetingPermit, *, title: str) -> dict[str, Any]:
        """Request device Qwen minutes, validate extractive claims, cache digest."""
        with self._lock:
            self._verify(permit, skill="qwen_minutes")
            packet = self.evidence(permit)
            draft = self._provider.suggest_minutes({
                "task_id": permit.task_id, "meeting_id": permit.meeting_id,
                "untrusted_evidence": list(packet),
                "policy": "Only cite exact segment text; no inferred owners/dates/identities.",
            })
            checked = validate_minutes_proposal(draft, packet, meeting_title=title)
            digest = hashlib.sha256(json.dumps(
                checked, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            packet_digest = hashlib.sha256(json.dumps(
                list(packet), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            self._validated_minutes[(permit.task_id, permit.meeting_id)] = (
                digest, packet_digest)
            return checked

    def export_pdf(self, permit: MeetingPermit, minutes: Any) -> dict[str, Any]:
        """Export validated minutes to a guarded PDF via the device provider."""
        with self._lock:
            self._verify(permit, skill="qwen_minutes")
            packet = self.evidence(permit)
            if type(minutes) is not dict:
                raise ValueError("Validated minutes object required")
            digest = hashlib.sha256(json.dumps(
                minutes, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            packet_digest = hashlib.sha256(json.dumps(
                list(packet), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            if self._validated_minutes.get((permit.task_id, permit.meeting_id)) != (
                    digest, packet_digest):
                raise PermissionError("PDF contents/evidence not validated for this task")
            handle = self._provider.save_pdf(permit.meeting_id, minutes, list(packet))
            if type(handle) is not str or not ARTIFACT.fullmatch(handle):
                raise ValueError("Invalid guarded PDF artifact ID")
            self._artifacts[(permit.meeting_id, handle)] = permit.task_id
            return {"artifact_id": handle, "delivery": "not_sent"}

    def deliver(self, permit: MeetingPermit, artifact_id: str, *,
                local_authorized: bool = False) -> dict[str, Any]:
        """Deliver a guarded PDF via QR/OBEX handoff; requires separate approval."""
        with self._lock:
            self._verify(permit, skill="qr_obex_handoff")
            if not local_authorized:
                raise PermissionError("QR/OBEX handoff requires separate local approval")
            if self._artifacts.get((permit.meeting_id, artifact_id)) != permit.task_id:
                raise PermissionError("Unknown or foreign artifact ID")
            output = self._provider.deliver_pdf(permit.meeting_id, artifact_id)
            if type(output) is not dict or type(output.get("delivered")) is not bool:
                raise ValueError("Invalid handoff result")
            return {"meeting_id": permit.meeting_id, "delivered": output["delivered"]}

    def revoke(self, permit: Any) -> None:
        """Revoke a permit, removing access to its meeting from this task."""
        if type(permit) is not MeetingPermit:
            raise PermissionError("Permit required")
        with self._lock:
            key = (permit.task_id, permit.meeting_id)
            if self._permits.get(key) is not permit:
                raise PermissionError("Only the exact trusted meeting permit can be revoked")
            self._permits.pop(key, None)
            self._validated_minutes.pop(key, None)

    def clear_session(self, session_id: str, owner: str) -> None:
        """Revoke all permits for *session_id*/*owner* without stopping recordings."""
        valid_id(session_id)
        if type(owner) is not str or not OWNER.fullmatch(owner):
            raise ValueError("Invalid owner")
        with self._lock:
            for key, p in tuple(self._permits.items()):
                if p.session_id == session_id and p.owner == owner:
                    self._permits.pop(key, None)
                    self._validated_minutes.pop(key, None)
            for key in tuple(self._artifacts):
                if (
                    key[0] in self._meetings
                    and self._meetings[key[0]]["session_id"] == session_id
                    and self._meetings[key[0]]["owner"] == owner
                ):
                    self._artifacts.pop(key, None)
