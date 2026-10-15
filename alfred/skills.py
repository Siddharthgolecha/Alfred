"""Issue #17: typed, NON-EXECUTABLE installed-skill discovery.

Skill metadata never becomes a tool schema, permission, shell command or
approval. Keep platform-specific imports out of main. Capability status is
probed from current dependencies, never inferred from repository branch name.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import shutil

_ID = re.compile(r"^[a-z][a-z0-9_]{2,39}$")


@dataclass(frozen=True)
class Skill:
    name: str
    category: str
    interface_version: int
    available: bool
    reason: str
    requires_consent: bool
    requires_local_approval: bool
    evidence_only: bool

    def __post_init__(self):
        if not isinstance(self.name, str) or not _ID.fullmatch(self.name):
            raise ValueError("Invalid skill ID")
        if self.category not in ("system", "workflow", "reminder", "meeting", "delivery"):
            raise ValueError("Unknown skill category")
        if type(self.interface_version) is not int or self.interface_version != 1:
            raise ValueError("Unsupported skill interface")
        if any(type(x) is not bool for x in (
            self.available, self.requires_consent,
            self.requires_local_approval, self.evidence_only)):
            raise ValueError("Invalid skill flags")
        if type(self.reason) is not str or len(self.reason) > 160:
            raise ValueError("Invalid availability reason")

    def metadata(self):
        return {"name":self.name, "category":self.category,
                "version":self.interface_version, "available":self.available,
                "reason":self.reason, "requires_consent":self.requires_consent,
                "requires_local_approval":self.requires_local_approval,
                "evidence_only":self.evidence_only}


class SkillRegistry:
    """Allowlisted capability inventory with zero executable entry points.

    `tools` is the existing Phase 4 Tools instance, not a plugin loader.
    `meeting` is an explicitly provisioned OfflineMeetingAdapter, otherwise
    absent Jetson devices/modules are reported unavailable.
    """

    NAMES = ("gpu_status", "health_graph", "reminders", "meeting_recording",
             "transcript_retrieval", "qwen_minutes", "qr_obex_handoff")

    def __init__(self, *, tools=None, meeting=None, gpu_probe=None):
        self._tools=tools
        self._meeting=meeting
        self._gpu_probe=gpu_probe if gpu_probe is not None else (
            lambda: shutil.which("tegrastats") is not None)
        if not callable(self._gpu_probe):
            raise ValueError("Invalid GPU capability probe")

    def _status(self, name, available, reason):
        meeting = name in ("meeting_recording", "transcript_retrieval",
                           "qwen_minutes", "qr_obex_handoff")
        return Skill(
            name, {
                "gpu_status":"system","health_graph":"workflow",
                "reminders":"reminder","meeting_recording":"meeting",
                "transcript_retrieval":"meeting","qwen_minutes":"meeting",
                "qr_obex_handoff":"delivery"}[name],
            1, bool(available), "ready" if available else reason,
            requires_consent=meeting,
            requires_local_approval=(name in ("health_graph","meeting_recording",
                                              "transcript_retrieval",
                                              "qwen_minutes","qr_obex_handoff")),
            evidence_only=(name in ("transcript_retrieval","qwen_minutes")))

    def _health_graph(self):
        if self._tools is None or not hasattr(self._tools,"workflow_engine"):
            return False,"Phase 4 workflow engine missing"
        # Metadata is not approval: report the skill available only when an
        # actual approved, named health graph exists in the live local store.
        definitions=self._tools.workflow_engine.definitions()
        if not isinstance(definitions,list):
            return False,"Workflow inventory unavailable"
        match=any(isinstance(item,dict) and item.get("name")=="health_graph"
                  and item.get("state")=="approved" for item in definitions)
        return match, "No approved health_graph workflow"

    def _meeting_available(self, operation):
        m=self._meeting
        if m is None:
            return False,"Offline meeting adapter not installed"
        probe=getattr(m,"available",None)
        if not callable(probe):
            return False,"Meeting adapter capability probe missing"
        # An implementation must explicitly attest readiness of this exact
        # capability (microphones/SSD/Qwen/PDF/OBEX as appropriate).
        try:
            available=probe(operation)
        except (OSError, RuntimeError, ValueError, TypeError):
            return False,"Meeting capability probe failed"
        if type(available) is not bool:
            return False,"Invalid meeting capability response"
        return available,"Required offline meeting capability unavailable"

    def list(self):
        try:
            gpu=self._gpu_probe()
            gpu= gpu if type(gpu) is bool else False
        except (OSError,RuntimeError,ValueError):
            gpu=False
        health=self._health_graph()
        reminders=(self._tools is not None
                   and hasattr(self._tools,"automations")
                   and hasattr(self._tools,"store")
                   and callable(getattr(self._tools.store,"create_reminder",None)))
        state=[
            self._status("gpu_status",gpu,"tegrastats or GPU device unavailable"),
            self._status("health_graph",*health),
            self._status("reminders",reminders,"Local reminder store unavailable")]
        for name in self.NAMES[3:]:
            state.append(self._status(name,*self._meeting_available(name)))
        return tuple(state)

    def get(self, name):
        if type(name) is not str or name not in self.NAMES:
            raise ValueError("Unknown skill")
        return next(skill for skill in self.list() if skill.name==name)

    def metadata(self):
        return [item.metadata() for item in self.list()]
