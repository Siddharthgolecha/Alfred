"""Bounded manual Needle 3 feedback with isolated per-task native conversations.

Do NOT call Needle.run(): its Python callbacks bypass Alfred's approval policy.
A first Cactus.route() resets the native conversation. Only safe, completed,
read-only results can be fed into the same agent via Cactus.route_feedback().
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import time

from .contracts import Decision, Observation
from ..system2 import preflight
from ..runtime import validate

# Never feed commands, transcripts, user/tool strings or arbitrary JSON to
# Needle as observations. A number cannot carry a prompt injection.
METRICS = frozenset({
    "available", "gpu_temp_c", "uptime_seconds", "root_free_gib"
})
READONLY = frozenset({
    "get_gpu_status", "get_system_status", "list_services",
})
PRONOUN = re.compile(r"^\s*(?:check|show|read|get)\s+(?:it|that)(?:\s+again)?\s*[.!?]*\s*$", re.I)
MULTISTEP = re.compile(r"\b(?:then|afterwards|after that|next|depending on|based on)\b", re.I)
GPU_TEMP = re.compile(r"\b(?:gpu|graphics\s+(?:card|processor))\b.*\btemp(?:erature)?\b|"
                      r"\btemp(?:erature)?\b.*\b(?:gpu|graphics\s+(?:card|processor))\b", re.I)


class ContextUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class LoopOutcome:
    result: dict
    accepted_calls: tuple
    stop: str
    observed: tuple


def _arguments(call):
    """Missing arguments means no-argument tool; a PRESENT value must be a dict."""
    if "arguments" not in call:
        return {}
    if type(call["arguments"]) is not dict:
        raise ValueError("Malformed Needle arguments")
    return call["arguments"]


def _fingerprint(call):
    return json.dumps({"name": call["name"], "arguments": _arguments(call)},
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded_metrics(value):
    """Whitelisted scalar telemetry only; never textual tool outputs."""
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key in sorted(METRICS & value.keys()):
        item = value[key]
        if type(item) is bool:
            safe[key] = item
        elif type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 10**9:
            safe[key] = item
    return safe


def _feedback_metrics(tool, value):
    if tool == "list_services":
        # The local registry returns a mapping of service name -> status. The
        # model only learns an integer count, never text from service responses.
        if type(value) is not dict or len(value) > 32:
            raise ValueError("Unsafe service list result")
        return {"service_count": len(value)}
    if tool not in ("get_gpu_status", "get_system_status"):
        raise ValueError("Feedback not supported for this tool")
    return _bounded_metrics(value)


class SessionSlots:
    """Short-lived, unambiguous and purely local *verified* GPU-temp referents."""
    def __init__(self, ttl_seconds=120, clock=time.monotonic):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("Invalid session slot TTL")
        self.ttl = ttl_seconds
        self.clock = clock
        self._slots = {}

    def clear(self, session_id=None):
        if session_id is None:
            self._slots.clear()
        else:
            self._slots.pop(session_id, None)

    def resolve(self, session_id, utterance):
        if not PRONOUN.fullmatch(utterance):
            return utterance
        item = self._slots.get(session_id)
        if item is None or item[0] != "gpu_temperature" or self.clock() - item[1] > self.ttl:
            self._slots.pop(session_id, None)
            raise ContextUnavailable("I can't tell what 'it' refers to. Please name the device or metric.")
        return "Show GPU temperature again"

    def remember(self, session_id, explicit_text, tool, result):
        """Update only from an explicitly named, successful local GPU reading.

        Never derive an entity from tool output, injected instructions, an
        unsupported model claim, another session, or an ambiguous reference.
        """
        if (GPU_TEMP.search(explicit_text) and tool == "get_gpu_status"
                and isinstance(result, dict)
                and type(result.get("available")) is bool and result["available"]
                and type(result.get("gpu_temp_c")) in (int, float)
                and math.isfinite(result["gpu_temp_c"])
                and -50 <= result["gpu_temp_c"] <= 150):
            self._slots[session_id] = ("gpu_temperature", self.clock())


class NeedleAgentLoop:
    """One bounded task. Consequential calls stop and remain System2-owned."""

    def __init__(self, system2, feedback, ledger=None, task_id=None,
                 *, confidence_floor=0.7, max_decisions=3, max_tools=3,
                 cancelled=None):
        if type(max_decisions) is not int or not 1 <= max_decisions <= 3:
            raise ValueError("needle decisions must be 1..3")
        if type(max_tools) is not int or not 1 <= max_tools <= 3:
            raise ValueError("needle tool calls must be 1..3")
        if (type(confidence_floor) not in (float, int)
                or not math.isfinite(confidence_floor) or not 0 < confidence_floor <= 1):
            raise ValueError("invalid confidence floor")
        self.system2 = system2
        self.feedback = feedback
        self.ledger = ledger
        self.task_id = task_id
        self.floor = confidence_floor
        self.max_decisions = max_decisions
        self.max_tools = max_tools
        self.cancelled = cancelled or (lambda: False)

    def _record(self, event):
        if self.ledger is not None:
            self.ledger.append(self.task_id, event)

    def _stop(self, why, accepted, observed, outputs):
        words = {
            "uncertain": "Needle was uncertain. Please clarify the next step.",
            "unsafe": "Needle produced an unsafe or invalid follow-up. Please clarify.",
            "repeated": "Repeated tool request stopped without executing it again.",
            "budget": "The bounded Needle task limit was reached. Please start a new request.",
            "cancelled": "The authorized task was cancelled before another tool call.",
            "feedback_unavailable": "Needle feedback inference failed after a completed read.",
        }
        if observed:
            # The *next* proposal failed, not any previously returned read.
            # Retain the actual stop cause for audit, but disclose the
            # completed prefix instead of disguising it as a total failure.
            return LoopOutcome({
                "message": "Partial results: " + words[why]
                           + " Earlier read-only results were returned; "
                             "no further tools were run.",
                "results": outputs,
                "partial": True,
                "halt_reason": why,
            }, tuple(accepted), why, tuple(observed))
        return LoopOutcome({"message": words[why], "results": outputs},
                           tuple(accepted), why, tuple(observed))

    def run(self, initial, *, permit_feedback=False):
        response = initial
        accepted, observed, outputs = [], [], []
        fingerprints = set()
        decisions = 0
        while True:
            if self.cancelled():
                return self._stop("cancelled", accepted, observed, outputs)
            decisions += 1
            if decisions > self.max_decisions:
                return self._stop("budget", accepted, observed, outputs)
            if not isinstance(response, dict):
                return self._stop("unsafe", accepted, observed, outputs)
            calls = response.get("function_calls") or []
            if not isinstance(calls, list):
                return self._stop("unsafe", accepted, observed, outputs)
            if decisions > 1:
                confidence = response.get("confidence")
                safe_confidence = (float(confidence) if type(confidence) in (int, float)
                                   and math.isfinite(confidence) and 0 <= confidence <= 1
                                   else None)
                self._record(Decision("needle3", "direct" if calls else "readonly",
                                      min(len(calls), 50), safe_confidence))
            if response.get("suppressed_calls") or (
                isinstance(response.get("validation"), dict)
                and response["validation"].get("ungrounded")
            ):
                return self._stop("unsafe", accepted, observed, outputs)
            if not calls:
                return LoopOutcome({"message":"Completed" if outputs else "No tool action required",
                                    "results": outputs}, tuple(accepted), "completed", tuple(observed))
            confidence = response.get("confidence")
            if (type(confidence) not in (float, int)
                    or not math.isfinite(confidence)
                    or not self.floor <= confidence <= 1):
                return self._stop("uncertain", accepted, observed, outputs)
            if len(accepted) + len(calls) > self.max_tools:
                return self._stop("budget", accepted, observed, outputs)
            if not all(isinstance(x, dict) and type(x.get("name")) is str and
                       ("arguments" not in x or type(x["arguments"]) is dict)
                       for x in calls):
                return self._stop("unsafe", accepted, observed, outputs)
            try:
                signatures = [_fingerprint(x) for x in calls]
            except (ValueError, TypeError):
                return self._stop("unsafe", accepted, observed, outputs)
            if len(signatures) != len(set(signatures)) or any(s in fingerprints for s in signatures):
                return self._stop("repeated", accepted, observed, outputs)
            if self.cancelled():
                return self._stop("cancelled", accepted, observed, outputs)
            # Every batch is preflighted BEFORE a single tool runs; this is
            # still System2's existing concrete allowlist and plan policy.
            if any(x["name"] not in READONLY for x in calls):
                if decisions > 1:
                    return self._stop("unsafe", accepted, observed, outputs)
                try:
                    for x in calls:
                        validate(x["name"], _arguments(x),
                                 allow_delegate=False)
                    # A graph workflow has its own digest-based approval path;
                    # all other actions undergo ordinary System2 preflight.
                    if not any(x["name"] == "run_workflow" for x in calls):
                        preflight([{"tool":x["name"],"args":_arguments(x)}
                                   for x in calls], self.system2.tools, self.max_tools)
                    elif len(calls) != 1:
                        return self._stop("unsafe", accepted, observed, outputs)
                except (ValueError, KeyError, TypeError, PermissionError):
                    return self._stop("unsafe", accepted, observed, outputs)
                # Preserve one-shot System2 policy, including run_workflow
                # digest staging and approval; DO NOT expose to Needle again.
                result = self.system2.direct(calls)
                accepted.extend(calls)
                return LoopOutcome(result, tuple(accepted), "protected", tuple(observed))
            try:
                preflight([{"tool": x["name"], "args": _arguments(x)}
                           for x in calls], self.system2.tools, self.max_tools)
            except (ValueError, PermissionError, KeyError, TypeError):
                return self._stop("unsafe", accepted, observed, outputs)
            result = self.system2.direct(calls)
            accepted.extend(calls)
            fingerprints.update(signatures)
            if "approval" in result:
                return LoopOutcome(result, tuple(accepted), "protected", tuple(observed))
            batch = result.get("results")
            if not isinstance(batch, list) or len(batch) != len(calls):
                return self._stop("unsafe", accepted, observed, outputs)
            outputs.extend(batch)
            for call in calls:
                self._record(Observation(call["name"], "returned"))
                observed.append(call["name"])
            if not permit_feedback or not all(call["name"] in READONLY for call in calls):
                # Safe but non-chainable listings execute once through System2
                # and do not feed opaque lists/objects into the model.
                return LoopOutcome({"message":result.get("message","Completed"),
                                    "results":outputs}, tuple(accepted), "completed", tuple(observed))
            if decisions >= self.max_decisions or len(accepted) >= self.max_tools:
                # A validated tool has ALREADY succeeded. Report it truthfully
                # as partial; never mark execution as a rejected/failed task.
                return LoopOutcome({
                    "message": "Partial results: validated read-only tools ran, "
                               "but the bounded feedback limit was reached. "
                               "Any further requested checks remain unverified.",
                    "results": outputs, "partial": True},
                    tuple(accepted), "partial", tuple(observed))
            if self.cancelled():
                return self._stop("cancelled", accepted, observed, outputs)
            # Model sees ONLY bounded scalar telemetry; arbitrary result strings
            # (including adversarial JSON/prompt injection) are discarded.
            try:
                feedback = {"observations": [
                    {"tool": call["name"],
                     "metrics": _feedback_metrics(call["name"], item)}
                    for call, item in zip(calls, batch)
                ]}
            except ValueError:
                return self._stop("unsafe", accepted, observed, outputs)
            try:
                response = self.feedback(json.dumps(feedback, sort_keys=True,
                                                   separators=(",", ":")))
            except Exception:
                # Inference may crash AFTER the read returned. Keep that
                # observation visible, do not replay or attempt an action.
                outcome = self._stop("feedback_unavailable", accepted, observed, outputs)
                return LoopOutcome({**outcome.result, "status": "incomplete"},
                                   outcome.accepted_calls, outcome.stop,
                                   outcome.observed)
