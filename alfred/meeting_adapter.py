"""Issue #17 offline meeting ADAPTER CONTRACT: main has no Jetson imports.

Strict scope, per-task consent, bounded redacted evidence and extractive minutes.
Only an explicitly installed trusted device adapter implements real recording,
Whistle, guarded SSD/PDF or QR/OBEX; this module cannot execute shell, write
paths, access network, or open historical meetings by itself.

Skill status is metadata, NOT authorization. Callers must be the trusted
wake-authorized/CLI layer, never model-proposed tool names.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import secrets
import threading
import time

from .cognitive.contracts import valid_id

MEETING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
SOURCE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
ARTIFACT = re.compile(r"^[A-Za-z0-9_-]{8,72}$")
OWNER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
MAX_SEGMENTS = 64
MAX_EVIDENCE_BYTES = 16000
SECRET = re.compile(r"(?i)(?:sk-[A-Za-z0-9]{8,}|bearer\s+[^\s]+|"
                    r"(?<![\w.-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SENSITIVE_COMMAND = re.compile(r"(?i)\b(?:run|install|execute|approve|ignore)\b")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
UNKNOWN_OWNER = "Unassigned"
UNKNOWN_DATE = "Not specified"


def _text(value,limit):
    if type(value) is not str:
        raise ValueError("Invalid evidence text")
    value=CONTROLS.sub(" ", value)
    value=SECRET.sub("[redacted]", value)
    return value[:limit]


def _clock_time(value):
    if type(value) is not str or len(value)>40:
        raise ValueError("Invalid timestamp")
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timezone is mandatory")
    return parsed.astimezone(timezone.utc)


def _canonical_utc(value):
    return _clock_time(value).isoformat(timespec="microseconds").replace(
        "+00:00", "Z").replace(".000000Z", "Z")


def sanitize_segments(raw):
    """Expose mic channels, overlaps, uncertainty and source timing, not identity.

    This is a bounded *current-meeting* packet only. It does not read files or
    discover old transcripts. Speaker labels are never verified person names.
    """
    if type(raw) not in (tuple,list) or not 0 < len(raw) <= MAX_SEGMENTS:
        raise ValueError("Expected 1..64 typed meeting segments")
    result=[]
    ids=set()
    for index,entry in enumerate(raw):
        if type(entry) is not dict:
            raise ValueError("Malformed meeting segment")
        source=entry.get("source")
        if type(source) is not str or not SOURCE.fullmatch(source):
            raise ValueError("Invalid microphone channel identifier")
        start,end=entry.get("start_utc"),entry.get("end_utc")
        canonical_start=_canonical_utc(start)
        canonical_end=_canonical_utc(end)
        if _clock_time(end)<_clock_time(start):
            raise ValueError("Meeting segment timestamps reversed")
        sid=entry.get("id")
        if type(sid) is not str or not _ID_PATTERN.fullmatch(sid) or sid in ids:
            raise ValueError("Invalid/duplicate meeting segment ID")
        ids.add(sid)
        uncertainty=entry.get("synchronization_uncertainty_ms")
        if uncertainty is not None and (
            type(uncertainty) not in (float,int) or not math.isfinite(uncertainty)
            or not 0<=uncertainty<=30000):
            raise ValueError("Invalid synchronization uncertainty")
        overlaps=entry.get("overlap_with",[])
        if (type(overlaps) is not list or len(overlaps)>16
            or any(type(x) is not str or not _ID_PATTERN.fullmatch(x) for x in overlaps)):
            raise ValueError("Invalid overlap links")
        for flag in ("likely_crosstalk","ambiguous_crosstalk"):
            if type(entry.get(flag,False)) is not bool:
                raise ValueError("Invalid crosstalk flag")
        raw_words=entry.get("words",[])
        if type(raw_words) is not list or len(raw_words)>48:
            raise ValueError("Unbounded word-timing evidence")
        words=[]
        for word in raw_words:
            if type(word) is not dict:
                raise ValueError("Invalid timestamped word")
            a,b=word.get("start_seconds"),word.get("end_seconds")
            if (type(a) not in (float,int) or type(b) not in (float,int)
                or not math.isfinite(a) or not math.isfinite(b)
                or not 0<=a<=b):
                raise ValueError("Invalid word timestamp")
            value=_text(word.get("word"),60)
            if not value:
                raise ValueError("Empty word")
            words.append({"word":value,"start_seconds":a,"end_seconds":b})
        row={
            "id":sid, "start_utc":canonical_start, "end_utc":canonical_end,
            "source":source,"speaker":source, "identity_type":"microphone_channel",
            "verified_identity":False,
            "text":_text(entry.get("text"),260),
            "overlap_with":list(overlaps),
            "overlap":bool(overlaps),
            "likely_crosstalk":entry.get("likely_crosstalk",False),
            "ambiguous_crosstalk":entry.get("ambiguous_crosstalk",False),
            "synchronization_uncertainty_ms":uncertainty,
            "timing_basis":"decoder_estimated",
            "words":words,
        }
        result.append(row)
    for row in result:
        if any(x not in ids or x==row["id"] for x in row["overlap_with"]):
            raise ValueError("Dangling or self-referential overlap link")
    if len(json.dumps(result,ensure_ascii=True))>MAX_EVIDENCE_BYTES:
        raise ValueError("Meeting evidence exceeds bounded local context")
    return tuple(result)


def _quote(raw,by_id):
    """Only a short verbatim span from an explicit cited mic segment is admissible."""
    if (type(raw) is not dict or set(raw)!={"segment_id","quote"}
            or type(raw["segment_id"]) is not str or raw["segment_id"] not in by_id):
        raise ValueError("Uncited minutes claim")
    q=_text(raw["quote"],300)
    source=by_id[raw["segment_id"]]["text"]
    if not 2<=len(q)<=300 or q.casefold() not in source.casefold():
        raise ValueError("Unsubstantiated minutes claim")
    return q,source


def validate_minutes_proposal(proposal, segments, *, meeting_title):
    """Reject unsupported facts; no owner, date, identity or decision synthesis.

    A Jetson Qwen adapter should request citeable segment IDs + exact quotes.
    Device PDF writer receives *normalized* safe minutes, not model-chosen paths.
    """
    allowed={"summary","agenda","key_points","decisions","action_items",
             "risks","open_questions"}
    if type(proposal) is not dict or set(proposal)!=allowed:
        raise ValueError("Invalid structured minutes proposal")
    title=_text(meeting_title,100)
    if not title.strip():
        raise ValueError("Meeting title required")
    by_id={item["id"]:item for item in segments}
    out={"title":title}
    for field in ("summary","agenda","key_points","decisions","risks","open_questions"):
        values=proposal[field]
        if type(values) is not list or len(values)>24:
            raise ValueError("Unbounded minutes section")
        verified=[_quote(value,by_id)[0] for value in values]
        out[field]="\n".join(verified) if field=="summary" else verified
    if not out["summary"]:
        out["summary"]="No evidence-backed summary available"
    actions=proposal["action_items"]
    if type(actions) is not list or len(actions)>20:
        raise ValueError("Invalid action items")
    out["action_items"]=[]
    for item in actions:
        if type(item) is not dict or set(item)!={"task","owner","due"}:
            raise ValueError("Invalid action item fields")
        task,_=_quote(item["task"],by_id)
        # Entity/date attribution MUST come from the cited action quote,
        # never from another sentence in the same transcript segment.
        owner=item["owner"]
        due=item["due"]
        if type(owner) is not str or len(owner)>80 or (
            owner!=UNKNOWN_OWNER and (not owner.strip() or owner.casefold() not in task.casefold())):
            raise ValueError("Owner must be explicitly supported or unassigned")
        if type(due) is not str or (
            due!=UNKNOWN_DATE and (
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}",due)
                or due not in task)):
            raise ValueError("Due date must be explicitly supported or unspecified")
        # Never turn a microphone channel label into a verified identity.
        if owner in {item["source"] for item in segments}:
            raise ValueError("Microphone channel is not a verified person")
        out["action_items"].append({
            "task":task, "owner":owner,"due":due,
            "source_segment_id":item["task"]["segment_id"],
            "evidence_quote":task,
        })
    return out


@dataclass(frozen=True)
class MeetingPermit:
    session_id: str
    task_id: str
    owner: str
    meeting_id: str
    consent: bool
    allow_retention: bool
    granted_at: float
    expires_at: float

    def __post_init__(self):
        valid_id(self.session_id)
        valid_id(self.task_id)
        if type(self.owner) is not str or not OWNER.fullmatch(self.owner):
            raise ValueError("Invalid meeting owner")
        if type(self.meeting_id) is not str or not MEETING_ID.fullmatch(self.meeting_id):
            raise ValueError("Invalid meeting ID")
        if type(self.consent) is not bool or not self.consent or type(self.allow_retention) is not bool:
            raise PermissionError("Explicit consent required")
        if (type(self.granted_at) not in (float,int)
            or type(self.expires_at) not in (float,int)
            or not math.isfinite(self.granted_at)
            or not math.isfinite(self.expires_at)
            or not 0<self.expires_at-self.granted_at<=3600):
            raise ValueError("Invalid meeting access duration")


class OfflineMeetingAdapter:
    """Optional provider boundary with per-task access and no auto-install.

    Providers are injected by trusted host integration (e.g. Jetson adapter
    compatibility PR). No global transcript enumeration or path-based reads.
    """

    OPERATIONS=frozenset(("meeting_recording","transcript_retrieval",
                          "qwen_minutes","qr_obex_handoff"))

    def __init__(self, provider=None, *, journal=None, clock=time.time):
        self._provider=provider
        self._journal=journal
        self._clock=clock
        self._lock=threading.RLock()
        self._meetings={}       # meeting_id -> trusted session/owner/retention
        self._permits={}        # (task_id,meeting_id) -> exact immutable permit
        self._artifacts={}      # (meeting_id,artifact_id) -> retained media handle
        self._validated_minutes={}  # (task_id,meeting_id) -> digest

    def available(self, skill):
        if skill not in self.OPERATIONS:
            raise ValueError("Unknown meeting capability")
        if self._provider is None:
            return False
        probe=getattr(self._provider,"capability",None)
        if not callable(probe):
            return False
        # All required primitives need explicit provider attestation. For
        # minutes, require device Qwen and the guarded PDF writer.
        try:
            required={
                "meeting_recording":("microphone","guarded_ssd","recording"),
                "transcript_retrieval":("transcription","guarded_ssd"),
                "qwen_minutes":("transcription","qwen","safe_pdf"),
                "qr_obex_handoff":("safe_pdf","qr_obex"),
            }[skill]
            return all(probe(x) is True for x in required)
        except (OSError,ValueError,RuntimeError,TypeError):
            return False

    def start(self, *, session_id, task_id, owner, consent, retention=False,
              local_authorized=False):
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
                self._provider,"rollback_start",None)):
            raise RuntimeError("Recording requires durable journal and token rollback")
        # Host-chosen opaque operation ID MUST be honored by the provider:
        # it may rollback_start(operation_id) even if recording startup
        # succeeded and its returned meeting ID is missing/invalid.
        with self._lock:
            operation_id=secrets.token_hex(16)
            self._journal.put(operation_id,session_id,task_id,owner,
                              retained=retention,state="starting")
            try:
                meeting_id=self._provider.start_recording(
                    consent=True,retention=retention,operation_id=operation_id)
                if type(meeting_id) is not str or not MEETING_ID.fullmatch(meeting_id):
                    raise ValueError("Device returned an invalid meeting ID")
                if meeting_id in self._meetings:
                    raise ValueError("Duplicate meeting ID")
                self._journal.put(operation_id,session_id,task_id,owner,
                                  meeting_id=meeting_id,retained=retention,
                                  state="recording")
                self._meetings[meeting_id]={
                    "session_id":session_id,"owner":owner,
                    "retention":retention,"active":True,
                    "operation_id":operation_id}
            except BaseException:
                # Never assume a failing provider didn't start the mic.
                # A verified token rollback is safe even for duplicate IDs;
                # an unsuccessful rollback remains in SQLite for recovery.
                self._journal.put(operation_id,session_id,task_id,owner,
                                  retained=retention,state="rollback_pending")
                try:
                    rolled_back=self._provider.rollback_start(operation_id) is True
                except Exception:
                    rolled_back=False
                if rolled_back:
                    self._journal.remove(operation_id)
                    raise
                raise RuntimeError(
                    "Recorder registration failed; rollback unverified; "
                    "authorized durable cleanup required")
        return {"meeting_id":meeting_id,"status":"recording",
                "identity":"mic_channel_not_person","retention":retention}

    def permit(self, *, session_id, task_id, owner, meeting_id, consent,
               local_authorized=False, ttl_seconds=600):
        if not local_authorized or consent is not True:
            raise PermissionError("Each task requires explicit meeting access consent")
        if type(ttl_seconds) is not int or not 1<=ttl_seconds<=3600:
            raise ValueError("Invalid consent TTL")
        valid_id(session_id)
        valid_id(task_id)
        with self._lock:
            meeting=self._meetings.get(meeting_id)
            if (meeting is None or meeting["session_id"]!=session_id
                or meeting["owner"]!=owner):
                raise PermissionError("Meeting is outside authorized session/owner")
            if not meeting["active"] and not meeting["retention"]:
                raise PermissionError("Nonretained meeting cannot be reopened")
            now=float(self._clock())
            p=MeetingPermit(session_id,task_id,owner,meeting_id,
                            True,meeting["retention"],now,now+ttl_seconds)
            self._permits[(task_id,meeting_id)]=p
            return p

    def _verify(self, p, *, active=False, skill=None):
        if type(p) is not MeetingPermit:
            raise PermissionError("Trusted permit required")
        with self._lock:
            if self._permits.get((p.task_id,p.meeting_id)) is not p:
                raise PermissionError("Meeting permit revoked or fabricated")
            meeting=self._meetings.get(p.meeting_id)
            if (meeting is None or meeting["owner"]!=p.owner
                or meeting["session_id"]!=p.session_id
                or meeting.get("state") in ("stopping","purge_pending")
                or (not meeting["active"] and not meeting["retention"])
                or float(self._clock())>=p.expires_at):
                raise PermissionError("Meeting permit expired or outside scope")
            if active and not meeting["active"]:
                raise PermissionError("Meeting is not active")
            if skill and not self.available(skill):
                raise RuntimeError("Device capability unavailable")

    def status(self, permit):
        # Atomic authorization + provider operation. Stronger than per-meeting:
        # revocation, stop and purge cannot race any in-flight transmission.
        with self._lock:
            self._verify(permit,active=True,skill="meeting_recording")
            response=self._provider.recording_status(permit.meeting_id)
            if type(response) is not dict or response.get("state") not in ("recording","stopping"):
                raise ValueError("Device returned invalid meeting status")
            # Only minimal metadata, no device stdout or recording path.
            return {"meeting_id":permit.meeting_id,"state":response["state"],
                    "identity":"mic_channel_not_person"}

    def stop(self, permit, *, local_authorized=False):
        if local_authorized is not True:
            raise PermissionError("Stopping a recording requires separate local authorization")
        # Atomic authorization + provider operation. Stronger than per-meeting:
        # revocation, stop and purge cannot race any in-flight transmission.
        with self._lock:
            self._verify(permit,active=True,skill="meeting_recording")
            if self._journal is None:
                raise RuntimeError("Recording cleanup journal unavailable")
            with self._lock:
                meeting=self._meetings[permit.meeting_id]
                operation_id=meeting["operation_id"]
                retained=meeting["retention"]
                # Journal before physical side effects. A process crash between
                # stop and discard can be recovered by a trusted local operator.
                self._journal.put(operation_id,permit.session_id,permit.task_id,
                                  permit.owner,meeting_id=permit.meeting_id,
                                  retained=retained,state="stopping")
                meeting["active"]=False
                meeting["state"]="stopping"
                if not retained:
                    # Nonretained evidence must be inaccessible throughout purge.
                    # Retained recordings keep their existing task-bound permit
                    # after a successful stop, as before.
                    self._revoke_meeting_access(permit.meeting_id)
                # Providers must implement idempotent stop; recording may already
                # have ended before interruption, so never start it again.
                self._provider.stop_recording(permit.meeting_id)
                if retained:
                    self._journal.put(
                        operation_id,permit.session_id,permit.task_id,
                        permit.owner,meeting_id=permit.meeting_id,
                        retained=True,state="stopped_retained")
                    meeting["state"]="stopped"
                    return {"meeting_id":permit.meeting_id,"state":"stopped",
                            "retained":True}
                self._journal.put(operation_id,permit.session_id,permit.task_id,
                                  permit.owner,meeting_id=permit.meeting_id,
                                  retained=False,state="purge_pending")
                meeting["state"]="purge_pending"
                self._discard_or_raise(permit.meeting_id)
                self._journal.remove(operation_id)
                meeting["state"]="purged"
                return {"meeting_id":permit.meeting_id,"state":"stopped",
                        "retained":False}

    def _revoke_meeting_access(self, meeting_id):
        for key in tuple(self._permits):
            if key[1]==meeting_id:
                del self._permits[key]
                self._validated_minutes.pop(key,None)
        for key in tuple(self._artifacts):
            if key[0]==meeting_id:
                del self._artifacts[key]

    def _discard_or_raise(self, meeting_id):
        discard=getattr(self._provider,"discard_meeting",None)
        try:
            verified=callable(discard) and discard(meeting_id) is True
        except Exception:
            verified=False
        if not verified:
            raise RuntimeError(
                "Recorder stopped, but secure meeting discard unverified; "
                "durable cleanup pending and authorized retry required")

    def recover_cleanup(self, *, session_id, owner, local_authorized=False):
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
        completed=[]
        with self._lock:
            for row in self._journal.pending(session_id,owner):
                op=row["operation_id"]
                mid=row["meeting_id"]
                state=row["state"]
                if state in ("starting","rollback_pending"):
                    rollback=getattr(self._provider,"rollback_start",None)
                    try:
                        ok=callable(rollback) and rollback(op) is True
                    except Exception:
                        ok=False
                    if not ok:
                        raise RuntimeError("Recorder startup rollback still unverified")
                    self._journal.remove(op)
                    completed.append({"operation_id":op,"state":"rolled_back"})
                    continue
                if not isinstance(mid,str) or not MEETING_ID.fullmatch(mid):
                    raise RuntimeError("Cleanup journal lacks valid meeting ID")
                # A crash may have happened after stop() but before journal
                # update. Provider stop_recording must be idempotent.
                if state in ("recording","stopping"):
                    self._journal.put(op,row["session_id"],row["task_id"],
                        owner,meeting_id=mid,retained=bool(row["retained"]),
                        state="stopping")
                    self._provider.stop_recording(mid)
                    if row["retained"]:
                        self._journal.put(
                            op,row["session_id"],row["task_id"],owner,
                            meeting_id=mid,retained=True,
                            state="stopped_retained")
                        completed.append({"meeting_id":mid,"state":"stopped_retained"})
                        if mid in self._meetings:
                            self._meetings[mid]["active"]=False
                            self._meetings[mid]["state"]="stopped"
                            self._revoke_meeting_access(mid)
                        continue
                    self._journal.put(op,row["session_id"],row["task_id"],
                        owner,meeting_id=mid,retained=False,
                        state="purge_pending")
                # Even if already stopped, repeat discard until device
                # explicitly attests it is gone; never reopen transcript.
                if mid in self._meetings:
                    self._meetings[mid]["active"]=False
                    self._meetings[mid]["state"]="purge_pending"
                self._revoke_meeting_access(mid)
                self._discard_or_raise(mid)
                self._journal.remove(op)
                if mid in self._meetings:
                    self._meetings[mid]["state"]="purged"
                completed.append({"meeting_id":mid,"state":"purged"})
        return completed

    def evidence(self, permit):
        # Atomic authorization + provider operation. Stronger than per-meeting:
        # revocation, stop and purge cannot race any in-flight transmission.
        with self._lock:
            self._verify(permit,skill="transcript_retrieval")
            # Only the exact explicitly consented ID; never list historical runs.
            return sanitize_segments(self._provider.get_segments(permit.meeting_id))

    def minutes(self, permit, *, title):
        # Atomic authorization + provider operation. Stronger than per-meeting:
        # revocation, stop and purge cannot race any in-flight transmission.
        with self._lock:
            self._verify(permit,skill="qwen_minutes")
            packet=self.evidence(permit)
            # Device Qwen receives a structurally bounded, untrusted data packet.
            # Main cannot guarantee model behavior, so validate extractive claims.
            draft=self._provider.suggest_minutes({
                "task_id":permit.task_id,"meeting_id":permit.meeting_id,
                "untrusted_evidence":list(packet),
                "policy":"Only cite exact segment text; no inferred owners/dates/identities."})
            checked=validate_minutes_proposal(draft,packet,meeting_title=title)
            digest=hashlib.sha256(json.dumps(
                checked,sort_keys=True,separators=(",",":"),ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            packet_digest=hashlib.sha256(json.dumps(
                packet,sort_keys=True,separators=(",",":"),ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            with self._lock:
                self._validated_minutes[(permit.task_id,permit.meeting_id)]=(
                    digest,packet_digest)
            return checked

    def export_pdf(self, permit, minutes):
        # Atomic authorization + provider operation. Stronger than per-meeting:
        # revocation, stop and purge cannot race any in-flight transmission.
        with self._lock:
            self._verify(permit,skill="qwen_minutes")
            packet=self.evidence(permit)
            if type(minutes) is not dict:
                raise ValueError("Validated minutes object required")
            digest=hashlib.sha256(json.dumps(
                minutes,sort_keys=True,separators=(",",":"),ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            packet_digest=hashlib.sha256(json.dumps(
                packet,sort_keys=True,separators=(",",":"),ensure_ascii=True
            ).encode("utf-8")).hexdigest()
            with self._lock:
                if self._validated_minutes.get((permit.task_id,permit.meeting_id))!=(
                    digest,packet_digest):
                    raise PermissionError("PDF contents/evidence not validated for this task")
            # Provider implementation MUST call existing GuardedSSD PDF writer and
            # return an opaque artifact ID, never an arbitrary destination path.
            handle=self._provider.save_pdf(permit.meeting_id,minutes,list(packet))
            if type(handle) is not str or not ARTIFACT.fullmatch(handle):
                raise ValueError("Invalid guarded PDF artifact ID")
            with self._lock:
                self._artifacts[(permit.meeting_id,handle)]=permit.task_id
            return {"artifact_id":handle,"delivery":"not_sent"}

    def deliver(self, permit, artifact_id, *, local_authorized=False):
        # Atomic authorization + provider operation. Stronger than per-meeting:
        # revocation, stop and purge cannot race any in-flight transmission.
        with self._lock:
            self._verify(permit,skill="qr_obex_handoff")
            if not local_authorized:
                raise PermissionError("QR/OBEX handoff requires separate local approval")
            with self._lock:
                if self._artifacts.get((permit.meeting_id,artifact_id)) != permit.task_id:
                    raise PermissionError("Unknown or foreign artifact ID")
            output=self._provider.deliver_pdf(permit.meeting_id,artifact_id)
            if type(output) is not dict or type(output.get("delivered")) is not bool:
                raise ValueError("Invalid handoff result")
            return {"meeting_id":permit.meeting_id,"delivered":output["delivered"]}

    def revoke(self, permit):
        if type(permit) is not MeetingPermit:
            raise PermissionError("Permit required")
        with self._lock:
            key=(permit.task_id,permit.meeting_id)
            if self._permits.get(key) is not permit:
                raise PermissionError("Only the exact trusted meeting permit can be revoked")
            self._permits.pop(key,None)
            self._validated_minutes.pop(key,None)

    def clear_session(self, session_id, owner):
        valid_id(session_id)
        if type(owner) is not str or not OWNER.fullmatch(owner):
            raise ValueError("Invalid owner")
        with self._lock:
            # Do not automatically stop a live recording through a passive
            # memory clear: stop requires authorized meeting task.
            for key,p in tuple(self._permits.items()):
                if p.session_id==session_id and p.owner==owner:
                    self._permits.pop(key,None)
                    self._validated_minutes.pop(key,None)
            for key in tuple(self._artifacts):
                if (key[0] in self._meetings and
                    self._meetings[key[0]]["session_id"]==session_id
                    and self._meetings[key[0]]["owner"]==owner):
                    self._artifacts.pop(key,None)
