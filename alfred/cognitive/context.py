"""Privacy-bounded three-tier context for Needle and Qwen (issue #16).

Tier 1: ephemeral within-task *verified* numeric observations.
Tier 2: per-session, scoped, short-lived entity/measurement/workflow slots.
Tier 3: explicit local-CLI-approved, opt-in retained preferences (never tools).

No audio/transcript history, raw tool text, generated model instructions or
meeting content is stored in these tables. This is not a model authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from .contracts import valid_id
from .needle_loop import GPU_TEMP, ContextUnavailable, _bounded_metrics

SCOPE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
FACT_KEY = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
FORBIDDEN = re.compile(r"secret|password|token|api_key|command|script|system|"
                       r"prompt|instruction|shell|tool|policy|approval|credential", re.I)
SENSITIVE_VALUE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9]{8,}|bearer\\s+\\S+|"
    r"ignore\\s+(?:previous|all)\\s+instructions|"
    r"(?:^|\\s)(?:sudo|curl|wget)\\s|rm\\s+-rf|https?://)")
REF = re.compile(r"^\s*(?:check|read|show|get)\s+(?:it|that)(?:\s+again)?[.!?]*\s*$",re.I)
DANGEROUS_REF = re.compile(r"^\s*(?:restart|stop|start|cancel|delete|run|"
                            r"approve|change|disable|enable)\s+(?:it|that)(?:\s+again)?[.!?]*\s*$",re.I)
EXPLICIT_SYSTEM = re.compile(r"\b(?:system|machine|computer)\s+(?:status|health)\b",re.I)
EXPLICIT_WORKFLOW = re.compile(r"\bworkflow\b",re.I)
READS = frozenset(("get_gpu_status","get_system_status","list_services"))
SLOT_TYPES = frozenset(("gpu_temperature","system_status","active_workflow"))
MAX_TASK_OBSERVATIONS = 3
MAX_FACTS = 8


def _scope(owner, mode, session_id):
    valid_id(session_id)
    if (type(owner) is not str or not SCOPE_RE.fullmatch(owner)
            or mode not in ("assistant","meeting")):
        raise ValueError("Invalid context scope")
    return owner, mode, session_id


def _safe_metrics(tool, output):
    if tool not in READS or type(output) is not dict:
        return None
    if tool == "list_services":
        if len(output) > 32:
            return None
        return {"service_count": len(output)}
    safe = _bounded_metrics(output)
    if tool == "get_gpu_status":
        # A failed or fabricated reading is not a trusted fresh referent.
        if (safe.get("available") is not True
                or type(safe.get("gpu_temp_c")) not in (float,int)
                or not -50 <= safe["gpu_temp_c"] <= 150):
            return None
    if not safe:
        return None
    return safe


@dataclass(frozen=True)
class Slot:
    kind: str
    metrics: dict
    provenance: str
    confidence: float
    observed_at: float
    expires_at: float
    task_id: str

    def public(self, now):
        return {"kind":self.kind,"metrics":dict(self.metrics),
                "provenance":self.provenance,"confidence":self.confidence,
                "age_seconds":max(0,int(now-self.observed_at)),
                "ttl_remaining_seconds":max(0,int(self.expires_at-now))}


class ContextManager:
    """Shared packet API. No tool authority; SQLite only for approved facts."""

    def __init__(self, store, *, enabled=False, persist_facts=False,
                 owner="local", mode="assistant", ttl=120,
                 budget_chars=1200, clock=time.time):
        if (type(enabled) is not bool or type(persist_facts) is not bool
                or persist_facts and not enabled
                or type(ttl) is not int or not 1 <= ttl <= 300
                or type(budget_chars) is not int or not 256 <= budget_chars <= 3200):
            raise ValueError("Invalid context configuration")
        _scope(owner,mode,"a"*32)
        self.store=store
        self.enabled=enabled
        self.persist_facts=persist_facts
        self.owner=owner
        self.mode=mode
        self.ttl=ttl
        self.budget_chars=budget_chars
        self.clock=clock
        self._lock=threading.RLock()
        self._slots={}
        self._tasks={}
        self._facts={}  # scope -> key -> tagged value (nonpersistent only)
        if persist_facts:
            self._protect_db()
            with self.store.lock, self.store.db:
                self.store.db.execute("PRAGMA secure_delete=ON")
                self.store.db.execute("""
                    CREATE TABLE IF NOT EXISTS context_approved_facts(
                        owner TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        provenance TEXT NOT NULL CHECK(provenance='user_approved'),
                        saved_at INTEGER NOT NULL,
                        PRIMARY KEY(owner,mode,key)
                    )""")

    def _protect_db(self):
        # Retained facts use the *existing* SQLite database. Avoid following
        # symlinks or silently retaining private values in a world-readable DB.
        # New DB files created by Store may be 0644 under default umask.
        path=Path(self.store.db.execute("PRAGMA database_list").fetchone()[2])
        meta=os.lstat(path)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid():
            raise PermissionError("Context database must be an owned regular file")
        os.chmod(path, 0o600)
        for suffix in ("-wal","-shm"):
            sibling=Path(str(path)+suffix)
            if sibling.exists():
                m=os.lstat(sibling)
                if not stat.S_ISREG(m.st_mode) or m.st_uid!=os.getuid():
                    raise PermissionError("Unsafe SQLite auxiliary file")
                os.chmod(sibling,0o600)

    def switch_scope(self, owner, mode):
        _scope(owner,mode,"a"*32)
        with self._lock:
            self.owner, self.mode=owner,mode

    def _key(self, session_id):
        return _scope(self.owner,self.mode,session_id)

    def start_task(self, session_id, task_id):
        valid_id(task_id)
        if not self.enabled:
            return
        with self._lock:
            self._tasks[(self._key(session_id),task_id)]=[]

    def observe(self, session_id, task_id, explicit_text, tool, output,
                *, confidence=1.0):
        """Only locally verified System2 result, never model-supplied claims."""
        if not self.enabled:
            return False
        valid_id(task_id)
        if (type(confidence) not in (int,float) or not math.isfinite(confidence)
                or not .7 <= confidence <= 1):
            return False
        metrics=_safe_metrics(tool,output)
        if metrics is None or not isinstance(explicit_text,str):
            return False
        explicit = (bool(GPU_TEMP.search(explicit_text)) if tool=="get_gpu_status"
                    else bool(EXPLICIT_SYSTEM.search(explicit_text))
                         if tool=="get_system_status"
                    else bool(re.search(r"\bservices?\b",explicit_text,re.I)))
        # A model-chosen status tool is not enough: the user must have
        # explicitly requested the corresponding metric/category.
        if not explicit:
            return False
        kind = ("gpu_temperature" if tool=="get_gpu_status"
                else "system_status" if tool=="get_system_status" else None)
        now=float(self.clock())
        if not math.isfinite(now):
            raise ValueError("Invalid clock")
        key=self._key(session_id)
        with self._lock:
            task_key=(key,task_id)
            if task_key not in self._tasks:
                return False
            if len(self._tasks[task_key])<MAX_TASK_OBSERVATIONS:
                self._tasks[task_key].append({
                    "tool":tool,"metrics":metrics,"provenance":"observed",
                    "confidence":confidence,"observed_at":now})
            if kind:
                slots=self._slots.setdefault(key,{})
                slots[kind]=Slot(kind,metrics,"observed",float(confidence),
                                 now,now+self.ttl,task_id)
                self._prune_slots(slots,now)
            return True

    def set_workflow_reference(self, session_id, task_id, explicit_text,
                               workflow_name, *, locally_approved=False):
        """Only a named, already locally approved workflow; NOT runnable via pronoun."""
        if not self.enabled or not locally_approved or not EXPLICIT_WORKFLOW.search(explicit_text):
            return False
        valid_id(task_id)
        if (type(workflow_name) is not str
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,40}",workflow_name)):
            return False
        now=float(self.clock())
        with self._lock:
            key=self._key(session_id)
            if (key,task_id) not in self._tasks:
                return False
            self._slots.setdefault(key,{})["active_workflow"]=Slot(
                "active_workflow",{"registered":True},"observed",1.0,
                now,now+self.ttl,task_id)
            return True

    def _prune_slots(self, slots, now):
        for name in list(slots):
            if slots[name].expires_at <= now:
                del slots[name]
        # Hard cap: no slot flooding beyond the supported entity taxonomy.
        for name in sorted(set(slots)-SLOT_TYPES):
            del slots[name]

    def invalidate_unrelated(self, session_id, explicit_text):
        if not self.enabled:
            return
        with self._lock:
            slots=self._slots.get(self._key(session_id),{})
            if REF.fullmatch(explicit_text) or DANGEROUS_REF.fullmatch(explicit_text):
                return
            if not GPU_TEMP.search(explicit_text):
                slots.pop("gpu_temperature",None)
            if not EXPLICIT_SYSTEM.search(explicit_text):
                slots.pop("system_status",None)
            if not EXPLICIT_WORKFLOW.search(explicit_text):
                slots.pop("active_workflow",None)

    def resolve(self, session_id, utterance):
        if not self.enabled:
            return utterance
        key=self._key(session_id)
        if DANGEROUS_REF.fullmatch(utterance):
            raise ContextUnavailable("Action reference is ambiguous. Name the exact target and action.")
        if not REF.fullmatch(utterance):
            return utterance
        with self._lock:
            slots=self._slots.get(key,{})
            self._prune_slots(slots,float(self.clock()))
            refs=[k for k in ("gpu_temperature","system_status") if k in slots]
            # Active workflows never become an authority for action pronouns.
            if len(refs)!=1:
                raise ContextUnavailable("Reference is stale or ambiguous; please name the metric.")
            return ("Show GPU temperature again" if refs[0]=="gpu_temperature"
                    else "Get system status again")

    def finish_task(self, session_id, task_id):
        if not self.enabled:
            return
        with self._lock:
            self._tasks.pop((self._key(session_id),task_id),None)

    def _validate_fact(self, key, value):
        if (type(key) is not str or not FACT_KEY.fullmatch(key)
                or FORBIDDEN.search(key)
                or type(value) is not str or not 1 <= len(value) <= 160
                or any(ord(ch)<32 or ord(ch)==127 for ch in value)
                or SENSITIVE_VALUE.search(value)):
            raise ValueError("Invalid retained fact. Use short non-sensitive preferences.")
        return key,value

    def set_explicit_fact(self, key, value, *, local_approval=False):
        if not self.enabled or not local_approval:
            raise PermissionError("Only an explicitly approved local action can retain facts")
        key,value=self._validate_fact(key,value)
        with self._lock:
            existing=self.list_facts()
            if key not in existing and len(existing)>=MAX_FACTS:
                raise ValueError("Persistent fact budget reached")
            now=int(self.clock())
            if self.persist_facts:
                with self.store.lock,self.store.db:
                    self.store.db.execute(
                        "INSERT INTO context_approved_facts "
                        "(owner,mode,key,value,provenance,saved_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(owner,mode,key) DO UPDATE SET "
                        "value=excluded.value,saved_at=excluded.saved_at",
                        (self.owner,self.mode,key,value,"user_approved",now))
            else:
                self._facts.setdefault((self.owner,self.mode),{})[key]={
                    "value":value,"provenance":"user_approved","saved_at":now}

    def list_facts(self):
        if not self.enabled:
            return {}
        with self._lock:
            if self.persist_facts:
                with self.store.lock:
                    rows=self.store.db.execute(
                        "SELECT key,value,provenance,saved_at FROM context_approved_facts "
                        "WHERE owner=? AND mode=? ORDER BY key LIMIT ?",
                        (self.owner,self.mode,MAX_FACTS+1)).fetchall()
                return {r["key"]:{"value":r["value"],"provenance":r["provenance"],
                                   "saved_at":r["saved_at"]} for r in rows[:MAX_FACTS]}
            return dict(self._facts.get((self.owner,self.mode),{}))

    def clear_facts(self, key=None, *, local_approval=False):
        if not local_approval:
            raise PermissionError("Fact deletion is a trusted local operation")
        if key is not None and (not isinstance(key,str) or not FACT_KEY.fullmatch(key)):
            raise ValueError("Invalid fact key")
        with self._lock:
            if self.persist_facts:
                with self.store.lock,self.store.db:
                    if key is None:
                        self.store.db.execute(
                            "DELETE FROM context_approved_facts WHERE owner=? AND mode=?",
                            (self.owner,self.mode))
                    else:
                        self.store.db.execute(
                            "DELETE FROM context_approved_facts WHERE owner=? AND mode=? AND key=?",
                            (self.owner,self.mode,key))
            else:
                facts=self._facts.get((self.owner,self.mode),{})
                if key is None:
                    facts.clear()
                else:
                    facts.pop(key,None)

    def clear_session(self, session_id):
        key=self._key(session_id)
        with self._lock:
            self._slots.pop(key,None)
            for item in list(self._tasks):
                if item[0]==key:
                    del self._tasks[item]

    def compact(self, session_id, task_id=None, *, for_needle=False):
        """Deterministic minimum-first packet, no model summary/authority."""
        if not self.enabled:
            return {}
        key=self._key(session_id)
        now=float(self.clock())
        with self._lock:
            slots=self._slots.get(key,{})
            self._prune_slots(slots,now)
            order=("gpu_temperature","system_status","active_workflow")
            subset={k:slots[k].public(now) for k in order if k in slots}
            observation=[]
            if task_id is not None:
                valid_id(task_id)
                observation=list(self._tasks.get((key,task_id),[]))
            # Needle receives typed measurements only, never retained fact text.
            packet={"version":1,"scope":self.mode,"slots":subset}
            if not for_needle:
                packet["task_observations"]=observation
                packet["user_approved_facts"]=self.list_facts()
            def size():
                return len(json.dumps(packet,sort_keys=True,
                                      separators=(",",":"),allow_nan=False))
            # Safest deterministic compaction: drop oldest/lowest priority
            # contextual data rather than ask a model to fabricate a summary.
            if size()>self.budget_chars and "task_observations" in packet:
                while packet["task_observations"] and size()>self.budget_chars:
                    packet["task_observations"].pop(0)
            if size()>self.budget_chars and "user_approved_facts" in packet:
                for k in sorted(packet["user_approved_facts"],reverse=True):
                    if size()<=self.budget_chars: break
                    del packet["user_approved_facts"][k]
            for k in reversed(order):
                if size()>self.budget_chars:
                    packet["slots"].pop(k,None)
            if size()>self.budget_chars:
                raise ValueError("Context packet budget below structural minimum")
            return packet

    def as_model_json(self, session_id, task_id=None, *, for_needle=False):
        return json.dumps(self.compact(session_id,task_id,for_needle=for_needle),
                          sort_keys=True,separators=(",",":"),allow_nan=False)
