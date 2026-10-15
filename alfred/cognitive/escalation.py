"""Issue #15: versioned, bounded Needle->Qwen task continuation.

The envelope is ephemeral. Never save user utterances, tool arguments or
observations in the append-only cognitive ledger or persistent approvals.
Qwen returns untrusted proposals: only System2 is authorized to act on them.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

from .contracts import valid_id
from .needle_loop import _feedback_metrics
from ..runtime import validate

VERSION = 1
MAX_HANDOFF_TOOLS = 5  # Protocol-v1 global per-task limit, including Needle reads.
MAX_REQUEST = 1200
MAX_CONTEXT = 3200
READS = frozenset(("get_gpu_status", "get_system_status", "list_services",
                   "list_reminders", "list_schedules", "list_workflows",
                   "list_automations"))
REASONS = frozenset(("unsupported", "delegated", "uncertain", "incomplete",
                     "explicit_reason"))
SAFE_RETRY = frozenset(("get_gpu_status", "get_system_status", "list_services"))


def _signature(name, args):
    return json.dumps({"tool":name, "args":args}, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class EscalationEnvelope:
    version: int
    session_id: str
    task_id: str
    request: str
    reason: str
    completed: tuple
    observations: tuple
    allow_actions: bool
    remaining_tools: int
    remaining_decisions: int

    def __post_init__(self):
        if type(self.version) is not int or self.version != VERSION:
            raise ValueError("Unsupported escalation protocol")
        valid_id(self.session_id)
        valid_id(self.task_id)
        if type(self.request) is not str or not 0 < len(self.request) <= MAX_REQUEST:
            raise ValueError("Invalid authorized request")
        if self.reason not in REASONS or type(self.allow_actions) is not bool:
            raise ValueError("Invalid handoff policy")
        if (type(self.remaining_tools) is not int
                or not 0 <= self.remaining_tools <= MAX_HANDOFF_TOOLS
                or type(self.remaining_decisions) is not int
                or not 0 <= self.remaining_decisions <= 3):
            raise ValueError("Invalid handoff budget")
        if (type(self.completed) is not tuple or type(self.observations) is not tuple
                or len(self.completed) > MAX_HANDOFF_TOOLS
                or len(self.completed) != len(self.observations)):
            raise ValueError("Invalid completed prefix")
        for call, observation in zip(self.completed, self.observations):
            if (type(call) is not dict or type(call.get("name")) is not str
                    or type(call.get("arguments", {})) is not dict):
                raise ValueError("Invalid completed call")
            validate(call["name"], call.get("arguments", {}), allow_delegate=False)
            if call["name"] not in READS:
                raise ValueError("No action-bearing side effects in handoff")
            if type(observation) is not dict:
                raise ValueError("Invalid sanitized observation")
            if call["name"] in SAFE_RETRY:
                expected_keys = set(_feedback_metrics(call["name"], observation))
                # For service-list, observation is already count only.
                if call["name"] == "list_services":
                    if set(observation) != {"service_count"} or (
                        type(observation["service_count"]) is not int
                        or not 0 <= observation["service_count"] <= 32
                    ):
                        raise ValueError("Invalid service count observation")
                elif set(observation) != expected_keys:
                    raise ValueError("Untrusted observation content")
            elif observation:
                raise ValueError("Opaque list result must not be given to Qwen")
        if len(self.to_model_context()) > MAX_CONTEXT:
            raise ValueError("Oversized handoff")

    def to_model_context(self):
        # No tool arguments, prompts, service names or raw results are sent.
        # JSON is data; the system prompt explicitly forbids taking instructions
        # from observed values or treating the context as authority.
        payload = {"version":self.version, "session_id":self.session_id,
                   "task_id":self.task_id, "handoff_reason":self.reason,
                   "allowed_actions":self.allow_actions,
                   "approval_policy":"trusted_local_cli_only",
                   "tool_policy":"System2_preflight_allowlist",
                   "remaining_tool_calls":self.remaining_tools,
                   "remaining_needle_decisions":self.remaining_decisions,
                   "completed_tools":[x["name"] for x in self.completed],
                   "observed_metrics":list(self.observations)}
        return json.dumps(payload,sort_keys=True,separators=(",",":"),allow_nan=False)

    def reconcile(self, steps):
        """Reject replay/new authority *before* System2 execution or staging."""
        if type(steps) is not list or len(steps) > self.remaining_tools:
            raise ValueError("Handoff exceeds remaining tool budget")
        previous = {_signature(c["name"], c.get("arguments", {})) for c in self.completed}
        now = set()
        for step in steps:
            if type(step) is not dict or type(step.get("tool")) is not str:
                raise ValueError("Invalid proposed step")
            key = _signature(step["tool"], step.get("args", {}))
            if key in previous or key in now:
                raise ValueError("Repeated tool proposal blocked before execution")
            now.add(key)
            # An untrusted model cannot turn a read-only handoff into a
            # reminder/service/task mutation. Local-only approval persists.
            if not self.allow_actions and step["tool"] not in READS:
                raise PermissionError("Read-only handoff cannot gain write authority")
        return steps


def build_handoff(session_id, task_id, request, reason, *,
                  calls=(), results=(), allow_actions=False,
                  max_tools=5, needle_budget=3, used_decisions=1):
    """Create ephemeral context from *trusted System2 results only*."""
    if type(calls) not in (tuple,list) or type(results) not in (tuple,list):
        raise ValueError("Invalid completed observations")
    if len(calls) != len(results):
        raise ValueError("Observation count mismatch")
    completed, safe = [], []
    for call, value in zip(calls, results):
        if type(call) is not dict or type(call.get("name")) is not str:
            raise ValueError("Invalid completed call")
        args = call.get("arguments", {})
        if type(args) is not dict:
            raise ValueError("Invalid completed arguments")
        validate(call["name"], args, allow_delegate=False)
        if call["name"] not in READS:
            # A completed side-effect cannot be safely re-planned by Qwen.
            raise PermissionError("Action-bearing task cannot be resumed")
        if call["name"] in SAFE_RETRY:
            if call["name"] == "list_services":
                if type(value) is not dict or len(value) > 32:
                    raise ValueError("Invalid service observations")
                data = {"service_count":len(value)}
            else:
                data = _feedback_metrics(call["name"],value)
        else:
            data = {}  # no raw lists or attacker-supplied names
        completed.append({"name":call["name"],"arguments":args})
        safe.append(data)
    if (type(allow_actions) is not bool or type(max_tools) is not int
            or not len(completed) <= max_tools <= MAX_HANDOFF_TOOLS
            or type(needle_budget) is not int or not 1 <= needle_budget <= 3
            or type(used_decisions) is not int
            or not 0 <= used_decisions <= needle_budget):
        raise ValueError("Invalid handoff policy or remaining budget")
    return EscalationEnvelope(
        VERSION, session_id, task_id, request, reason,
        tuple(completed), tuple(safe), allow_actions,
        max_tools-len(completed),
        max(0, min(3,needle_budget-used_decisions)))
