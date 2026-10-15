"""Alfred voice orchestrator — Alfred class and Feedback.

CLI entry-points (doctor, _main, main) live in alfred.cli.
Routing helpers (adjust_status_calls) are inlined here from alfred.routing.
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import logging
import math
from pathlib import Path
import re
import threading
import time

from .constants import Limits, Truncation

from .audio import AudioManager, AudioUnavailableError, UtteranceStream, pactl_json
from .console import TranscriptDisplay
from .config import data_path, load_config
from .feedback import Feedback  # extracted module; re-exported below
from .models import Cactus, LlamaSupervisor, Qwen
from .inference_admission import InferenceAdmission, AdmissionRejected
from .lifecycle import ChildProcesses, ShutdownSocket, request_close
from .runtime import SCHEMA_BY_NAME, TOOL_SCHEMAS, Scheduler, Store, Tools
from .system2 import System2, VerifiedRead, parse_plan
from .wakeword import WakeWordGate, AcousticWakeDetector
from .skills import SkillRegistry
from .cognitive import (SessionLedger, Decision, Observation, Escalation,
                        ApprovalReference, Completion, Diagnostic)
from .cognitive.harness_sdk import LocalHarness, HarnessUnavailable
from .cognitive.escalation import build_handoff, MAX_HANDOFF_TOOLS
from .cognitive.needle_loop import (NeedleAgentLoop, LoopOutcome, READONLY,
                                   SessionSlots, ContextUnavailable, MULTISTEP)
from .cognitive.context import ContextManager

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GPU-status routing helpers (inlined from alfred.routing)
# alfred.routing is kept as a re-export shim for backward compatibility.
# ---------------------------------------------------------------------------

_GPU = re.compile(r"\b(?:gpu|graphics\s+(?:card|processor))\b", re.I)
_STATUS = re.compile(
    r"\b(?:status|temp(?:erature)?|usage|utili[sz]ation|load|health|"
    r"metric(?:s)?|memory|vram|frequency|clock|power)\b", re.I
)
_OTHER_COMPONENT = re.compile(
    r"\b(?:cpu|system|overall|whole\s+device|ram|disk|storage)\b", re.I)
_EXCLUSION = re.compile(
    r"\b(?:why|explain|compare|interpret|cause|reason|"
    r"don't|do\s+not|never|without|instead\s+of|"
    r"remind|reminder|schedule|timer|stop|start|restart|turn\s+off|delete)\b",
    re.I)


def explicit_gpu_status_request(utterance: str) -> bool:
    """True only for straightforward spoken GPU status/metrics requests."""
    return bool(
        isinstance(utterance, str)
        and _GPU.search(utterance)
        and _STATUS.search(utterance)
        and not _EXCLUSION.search(utterance)
    )


def adjust_status_calls(utterance, calls):
    """Return a copied tool-call list; change only Needle status-only decisions."""
    if not explicit_gpu_status_request(utterance):
        return calls
    if not isinstance(calls, list) or any(
        not isinstance(call, dict) or call.get("name") not in
        ("get_gpu_status", "get_system_status") for call in calls
    ):
        return calls
    if not calls:
        return [{"name": "get_gpu_status", "arguments": {}}]
    mixed = bool(_OTHER_COMPONENT.search(utterance))
    adjusted = []
    for c in calls:
        if c["name"] == "get_system_status" and not mixed:
            adjusted.append({"name": "get_gpu_status", "arguments": {}})
        else:
            adjusted.append(dict(c))
    if not any(c["name"] == "get_gpu_status" for c in adjusted):
        adjusted.append({"name": "get_gpu_status", "arguments": {}})
    deduplicated = []
    names: set[str] = set()
    for item in adjusted:
        if item["name"] not in names:
            names.add(item["name"])
            deduplicated.append(item)
    return deduplicated

class Alfred:
    def __init__(self,cfg,load_models=True,*,meeting_adapter=None):
        self.cfg = cfg
        runtime = cfg["runtime"]
        self.store = Store(runtime["database"])
        self.sessions = SessionLedger(self.store)
        self.session_id = None
        self._command_source = "text"
        self._needle_task_lock = threading.RLock()
        self.needle_slots = SessionSlots(runtime["needle_context_ttl_seconds"])
        self.context = ContextManager(
            self.store,enabled=runtime["context_enabled"],
            persist_facts=runtime["context_persistence_enabled"],
            owner=runtime["context_owner"],mode=runtime["context_mode"],
            ttl=runtime["context_ttl_seconds"],
            budget_chars=runtime["context_budget_chars"])
        self._active_task_id = None
        self._needle_cancel = threading.Event()
        self.processes = ChildProcesses()
        self.admission = InferenceAdmission(
            enabled=runtime["resource_admission_enabled"],
            min_available_mib=runtime["resource_min_available_mib"],
            max_swap_used_mib=runtime["resource_max_swap_used_mib"],
            max_cpu_load_per_core=runtime["resource_max_cpu_load_per_core"],
            max_wait_ms=runtime["resource_max_wait_ms"])
        self.tools = Tools(runtime,self.store,processes=self.processes)
        # Discovery only: not a model tool, executor or device authority.
        self.skills = SkillRegistry(tools=self.tools,meeting=meeting_adapter)
        self.feedback = Feedback(runtime["tts"],processes=self.processes)
        self.audio = AudioManager(cfg["audio"])
        self.wake_word = WakeWordGate(cfg["wake_word"])
        self.supervisor = LlamaSupervisor(cfg["qwen"],runtime["logs"])
        self.qwen = (Qwen(cfg["qwen"],self.supervisor,admission=self.admission)
                     if self.admission.enabled
                     else Qwen(cfg["qwen"],self.supervisor))
        self.system2 = System2(self.qwen,self.tools,self.store,runtime)
        # Always use the actual, required Cordis SDK for Qwen cognition.
        # The privileged tool/approval gateway stays strictly in Alfred.
        model = cfg["qwen"]["model"] or cfg["qwen"]["expected_model"]
        self.harness = LocalHarness(
            self.supervisor.url, model,
            timeout=runtime["harness_timeout_seconds"],
            **({"admission":self.admission} if self.admission.enabled else {}))
        if not runtime.get("telemetry",False):
            import os
            os.environ.setdefault("NEEDLE_TELEMETRY","0")
        self.cactus = (Cactus(TOOL_SCHEMAS,admission=self.admission)
                       if load_models and self.admission.enabled else
                       Cactus(TOOL_SCHEMAS) if load_models else None)
        self.scheduler = Scheduler(self.store,self.tools,self._remind)
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        self._closed = False

    def _remind(self,event):
        text=f"Reminder #{event['id']}: {event['message']}"
        self.feedback.tell(text)
        self.store.event("reminder",event)

    def start_services(self):
        self.scheduler.start()
        if self.cfg["qwen"]["enabled"]:
            def watchdog():
                while not self._watchdog_stop.is_set():
                    try:
                        if self.admission.enabled:
                            if not self.admission.capture_active:
                                # Capture deferral must not busy-spin past stop wait.
                                with self.admission.claim("qwen"):
                                    self.supervisor.ensure_running()
                                LOG.info("Qwen available: %s", self.supervisor.status())
                        else:
                            self.supervisor.ensure_running()
                            LOG.info("Qwen available: %s", self.supervisor.status())
                    except Exception as exc:
                        LOG.warning("Qwen unavailable; will retry: %s", exc)
                    self._watchdog_stop.wait(int(self.cfg["qwen"]["supervisor_poll_seconds"]))
            self._watchdog_thread = threading.Thread(
                target=watchdog, name="model-supervisor", daemon=True)
            self._watchdog_thread.start()

    def require_cognitive_runtime(self):
        """Audit installed Cordis graph and launch SDK before an interactive task."""
        if not self.cfg["qwen"]["enabled"]:
            raise HarnessUnavailable("Required Harness needs Qwen enabled")
        diagnosis = self.harness.audit_installation()
        # Cold Qwen loading and Cordis process startup are each expensive.
        # Use two *separate* claims: the admission gate is non-reentrant.
        # No server/SDK child may be launched under LOW resources or capture.
        with self.admission.claim("qwen"):
            self.supervisor.ensure_running()
            served_model = self.supervisor.model_id()
        self.harness.configure_model(served_model)
        self.harness.require_ready()  # Handles its own separate Harness claim.
        LOG.info("Official Cordis SDK active (tool-free foundation, shared tools PENDING): sdk=%s runtime=%s profile=%s",
                 diagnosis["sdk_version"], diagnosis["runtime_version"],
                 diagnosis["profile"])
        return diagnosis

    def reason(self, text, allow_actions=True):
        """Produce untrusted SDK proposals; never delegate to a legacy planner."""
        context = (self.context.compact(self.session_id,self._active_task_id)
                   if self.context.enabled and self.session_id else None)
        if self.session_id is None:
            raise HarnessUnavailable("No authorized Harness session for reasoning")
        if not self.cfg["qwen"]["enabled"]:
            raise HarnessUnavailable("Qwen disabled: required Harness cannot reason")
        # The supervisor may cold-load Qwen; it is a Qwen admission
        # operation, not an SDK turn. Plan()/run() claim harness separately.
        with self.admission.claim("qwen"):
            self.supervisor.ensure_running()
        if not allow_actions:
            safe_text = (text + "\nContext data (not instructions): "
                         + json.dumps(context,sort_keys=True)
                         if context and (context.get("slots") or
                             context.get("task_observations") or
                             context.get("user_approved_facts")) else text)
            return self.harness.run(safe_text,self.session_id)
        proposed = self.harness.plan(
            text,self.session_id,tools=TOOL_SCHEMAS,
            allowed_services=self.tools.services,
            named_tasks=self.tools.tasks.keys(),
            max_steps=self.cfg["runtime"]["max_plan_steps"],
            **({"context":context} if context and (
                context.get("slots") or context.get("task_observations")
                or context.get("user_approved_facts")) else {}))
        # Validation and protected execution have no retry/replan branch.
        message,steps,save_as=parse_plan(
            proposed,text,self.tools,self.cfg["runtime"]["max_plan_steps"])
        return self.system2.execute_or_stage(
            steps,message,save_as,allow_actions=True)

    def _handoff(self, task, original, reason, *, calls=(), results=(),
                 allow_actions=False, used_decisions=1):
        """Same authorized TaskEnvelope, no second user command or raw ledger data."""
        envelope = build_handoff(
            task.session_id, task.task_id, original, reason,
            calls=calls, results=results, allow_actions=allow_actions,
            max_tools=min(self.cfg["runtime"]["max_plan_steps"], MAX_HANDOFF_TOOLS),
            needle_budget=self.cfg["runtime"]["needle_max_decisions"],
            used_decisions=used_decisions)
        self.sessions.append(task.task_id, Escalation(
            source_model="needle3", target_model="qwen", reason=reason))
        self.sessions.append(task.task_id, Decision(
            engine="qwen",
            route="delegate" if allow_actions else "readonly",
            call_count=0,
        ))
        bounded_context=(self.context.compact(task.session_id,task.task_id)
                         if self.context.enabled else None)
        return self.system2.continue_handoff(
            envelope, harness=self.harness, supervisor=self.supervisor,
            admission=self.admission,
            **({"session_context":bounded_context} if bounded_context else {}))

    def _observe_needle_prefix(self, task, original, outcome):
        """Preserve verified Needle-loop reads before an early handoff/stop.

        NeedleAgentLoop records 'observed' names only AFTER System2.direct
        returns a complete, aligned read-only batch. Neither model proposals
        nor an unmatched/failed/protected batch can become context evidence.
        Only ContextManager's explicit-category and bounded metric whitelist
        decides whether a returned value deserves a session slot.
        """
        if not self.context.enabled or type(outcome) is not LoopOutcome:
            return
        calls = outcome.accepted_calls
        names = outcome.observed
        values = outcome.result.get("results")
        if (type(calls) is not tuple or type(names) is not tuple
                or type(values) is not list
                or not (len(calls) == len(names) == len(values))):
            # Never infer a successful read from an unmatched result list.
            return
        for call, name, value in zip(calls, names, values):
            if (type(call) is not dict or type(name) is not str
                    or call.get("name") != name or name not in READONLY
                    or (type(value) is dict and value.get("skipped") is True)):
                continue
            self.context.observe(task.session_id, task.task_id,
                                 original, name, value, confidence=1.0)

    def _finish_handoff(self, task, result, original):
        """Consume typed handoff reads on the SAME task and finish safely.

        Only System2's post-execution VerifiedRead channel can update context:
        never infer reads from model text, proposed steps, or prefix summaries.
        Strip the private channel before any user-facing serialization.
        """
        verified = result.pop("_verified_reads", ())
        if (self.context.enabled and "approval" not in result
                and result.get("status") != "incomplete"
                and type(verified) is tuple):
            for item in verified:
                if (type(item) is VerifiedRead
                        and not (isinstance(item.output, dict)
                                 and item.output.get("skipped") is True)):
                    self.context.observe(task.session_id, task.task_id,
                                         original, item.tool, item.output,
                                         confidence=1.0)
        if "approval" in result:
            proposal=result["approval"]
            self.sessions.append(task.task_id,
                                 ApprovalReference(approval_id=proposal["approval_id"]))
            for name in result.get("proposed_tools", ()):
                self.sessions.append(task.task_id, Observation(tool=name, outcome="staged"))
            self.sessions.append(task.task_id,
                                 Completion(outcome="pending_approval", reason="needs_approval"))
            self.feedback.tell(
                f"Local approval required for {proposal['actions']}. "
                f"On the Jetson, run: alfred --approve {proposal['approval_id']}")
        elif result.get("status") == "incomplete":
            # Failed model responses cannot count as successful no-ops.
            # Keep prior read-only results visible and use a typed ledger
            # failure without persisting untrusted model/error text.
            reason = ("invalid_plan" if result.get("error") == "invalid_plan"
                      else "disabled" if not self.cfg["qwen"]["enabled"]
                      else "exception")
            self.sessions.append(task.task_id, Completion(outcome="failed", reason=reason))
            self.feedback.tell(json.dumps(result,default=str)[:Truncation.RESULT * 3 // 2])
        else:
            for item in result.get("tool_outcomes", ()):
                self.sessions.append(task.task_id, Observation(
                    tool=item["tool"], outcome=item["outcome"]))
            self.sessions.append(task.task_id, Completion(
                outcome="completed",
                reason="executed" if result.get("results") else "no_action",
            ))
            self.feedback.tell(json.dumps(result,default=str)[:Truncation.RESULT * 3 // 2])

    def resume_session(self, session_id):
        """Explicit local restart recovery; never restart an uncertain action."""
        with self._needle_task_lock:
            if self.session_id is not None:
                raise ValueError("Cannot change sessions while active")
            self.sessions.resume_session(session_id)
            self.session_id = session_id
            self.needle_slots.clear()
            return session_id

    def _start_task(self, source, input_chars):
        """One durable ID per authorized request, reused for every decision."""
        envelope = self.sessions.begin_task(source, input_chars, self.session_id)
        self.session_id = envelope.session_id
        self._active_task_id = envelope.task_id
        return envelope

    def handle_text(self,text):
        # Hold ownership from native reset() until final feedback and result.
        # Other authorized requests cannot reset the shared Needle agent
        # while this task's synchronous complete()/System2 calls are live.
        with self._needle_task_lock:
            return self._handle_text_locked(text)

    def _handle_text_locked(self,text):
        text=text.strip()
        if not text:
            return
        task = self._start_task(self._command_source, len(text))
        self.context.start_task(task.session_id,task.task_id)
        try:
            return self._process_authorized_task(task,text)
        finally:
            self.context.finish_task(task.session_id,task.task_id)
            self._active_task_id = None

    def _process_authorized_task(self,task,text):
        self.store.event("utterance",{"length":len(text)})  # No raw transcript stored.
        # This is a non-privileged local privacy command *after* voice wake
        # authorization. Never let a model reinterpret it as a tool action.
        if (self.context.enabled and
                re.fullmatch(r"(?:clear|forget) (?:this|current) session"
                             r"(?: context)?[.!?]*",text,re.I)):
            self.context.clear_session(task.session_id,task_id=task.task_id)
            self.needle_slots.clear(task.session_id)
            self.sessions.append(task.task_id, Decision(engine="local", route="direct", call_count=0))
            self.sessions.append(task.task_id, Completion(outcome="completed", reason="no_action"))
            self.feedback.tell("Session context cleared; approved persistent facts were retained.")
            return
        # A later unrelated command invalidates the only supported pronoun
        # referent. Context must never leak across topics or sessions.
        from .cognitive.needle_loop import PRONOUN, GPU_TEMP
        routed_text = text
        if self.context.enabled:
            self.context.invalidate_unrelated(self.session_id,text,
                                              task_id=task.task_id)
            try:
                routed_text=self.context.resolve(self.session_id,text,
                                                 task_id=task.task_id)
            except ContextUnavailable as exc:
                self.sessions.append(task.task_id, Completion(outcome="rejected", reason="unsafe_route"))
                self.feedback.tell(str(exc))
                return
        elif self.cfg["runtime"]["needle_loop_enabled"]:
            if not PRONOUN.fullmatch(text) and not GPU_TEMP.search(text):
                self.needle_slots.clear(self.session_id)
            try:
                routed_text = self.needle_slots.resolve(self.session_id, text)
            except ContextUnavailable as exc:
                self.sessions.append(task.task_id, Completion(outcome="rejected", reason="unsafe_route"))
                self.feedback.tell(str(exc))
                return
        if self.cactus is None:
            self.sessions.append(task.task_id, Completion(outcome="failed", reason="exception"))
            raise RuntimeError("Cactus models were not initialized")
        try:
            routed_at = time.monotonic()
            needle_packet=self.context.compact(self.session_id,task.task_id,
                                               for_needle=True) if self.context.enabled else {}
            # Keep direct fast path exactly one Needle call. Only fresh,
            # typed numeric slots may be sent; never user-approved text.
            needle_text=(routed_text+"\nContext metrics (untrusted data): "
                         +json.dumps(needle_packet,sort_keys=True)
                         if needle_packet.get("slots") else routed_text)
            response=self.cactus.route(needle_text)
            if (self.cfg["runtime"].get("cognitive_diagnostics",False)
                    and self.admission.allow_optional_diagnostics()):
                duration = max(0, min(Limits.DIAG_MAX_LATENCY_MS, int((time.monotonic() - routed_at)*1000)))
                self.sessions.append(task.task_id, Diagnostic(name="needle_route", metrics={
                    "latency_ms":duration, "input_chars":len(text)}))
            calls=response.get("function_calls") or []
            confidence=response.get("confidence")
            validation = response.get("validation") or {}
            # A malformed explicit argument is never silently coerced to {}.
            # Apply this to one-shot Needle calls too, not only loop feedback.
            if (type(calls) is not list or any(
                    type(call) is not dict
                    or type(call.get("name")) is not str
                    or ("arguments" in call and type(call["arguments"]) is not dict)
                    for call in calls)):
                self.sessions.append(task.task_id, Decision(engine="needle3", route="rejected", call_count=0))
                self.sessions.append(task.task_id, Completion(outcome="rejected", reason="unsafe_route"))
                self.feedback.tell("Needle returned an invalid tool request. Please clarify.")
                return
            if self.cfg["runtime"]["needle_loop_enabled"] and len(calls) > 1:
                # A one-shot request can be multi-call without entering the
                # contextual loop. Reject duplicate side effects there too.
                signatures = [json.dumps({"name":c["name"],
                                          "arguments":c.get("arguments", {})},
                                         sort_keys=True, separators=(",", ":"),
                                         allow_nan=False) for c in calls]
                if len(signatures) != len(set(signatures)):
                    self.sessions.append(task.task_id, Decision(engine="needle3", route="rejected", call_count=0))
                    self.sessions.append(task.task_id, Completion(outcome="rejected", reason="unsafe_route"))
                    self.feedback.tell("Repeated tool request rejected before execution.")
                    return
            if response.get("suppressed_calls") or (
                isinstance(validation, dict) and validation.get("ungrounded")
            ):
                self.sessions.append(task.task_id, Decision(
                    engine="needle3", route="rejected", call_count=min(len(calls), 50)))
                self.sessions.append(task.task_id, Completion(outcome="rejected", reason="unsafe_route"))
                self.feedback.tell("I couldn't safely determine that command. Please clarify.")
                return
            floor=float(self.cfg["runtime"]["needle_confidence_floor"])
            if calls and (type(confidence) not in (float, int)
                          or not math.isfinite(confidence) or not floor <= confidence <= 1):
                self.store.event("route_uncertain",{
                    "confidence": confidence, "call_count": len(calls)})
                self.sessions.append(task.task_id, Decision(
                    engine="needle3", route="uncertain", call_count=min(len(calls), 50)))
                if self.cfg["runtime"]["escalation_enabled"]:
                    outcome=self._handoff(task, text, "uncertain",allow_actions=False)
                    self._finish_handoff(task, outcome, text)
                    return
                self.sessions.append(task.task_id, Completion(outcome="rejected", reason="low_confidence"))
                self.feedback.tell("Command uncertain. Please repeat or be more specific.")
                return
            corrected=adjust_status_calls(routed_text,calls)
            if corrected is not calls:
                self.store.event("routing_adjustment",{
                    "reason":"explicit_gpu_status",
                    "original":[c.get("name") for c in calls],
                    "selected":[c.get("name") for c in corrected]
                })
                calls=corrected
            if (self.cfg["runtime"]["escalation_enabled"] and
                    any(call["name"] not in SCHEMA_BY_NAME for call in calls)):
                # An unknown name is not a tool grant. Never pass it to
                # System2.direct(), and never silently reinterpret the name
                # as a privileged service/command. Only read-only clarification
                # is permitted with the original wake-authorized request.
                self.sessions.append(task.task_id, Decision(
                    engine="needle3", route="rejected", call_count=min(len(calls), 50)))
                outcome=self._handoff(task, text, "unsupported", allow_actions=False)
                self._finish_handoff(task, outcome, text)
                return
            self.store.event("route", {
                "model":"needle3", "confidence":confidence,
                "call_count":len(calls),
                "delegated":bool(not calls or (len(calls)==1 and
                    calls[0].get("name")=="delegate_to_reasoner"))
            })
            if (self.cfg["runtime"]["needle_loop_enabled"]
                    and len(calls) > self.cfg["runtime"]["needle_max_tools"]):
                self.sessions.append(task.task_id, Completion(outcome="rejected", reason="unsafe_route"))
                self.feedback.tell("Needle exceeded the per-task tool budget. Please clarify.")
                return
            route = ("readonly" if not calls else
                     "delegate" if len(calls)==1 and
                     calls[0].get("name")=="delegate_to_reasoner" else "direct")
            safe_confidence = (float(confidence) if type(confidence) in (float,int)
                               and 0 <= confidence <= 1 else None)
            self.sessions.append(task.task_id, Decision(
                engine="needle3", route=route,
                call_count=min(len(calls), 50), confidence=safe_confidence))
            loop_outcome = None
            if (self.cfg["runtime"]["needle_loop_enabled"] and
                    calls and route == "direct" and MULTISTEP.search(text)):
                loop = NeedleAgentLoop(
                    self.system2, self.cactus.route_feedback, self.sessions,
                    task.task_id,
                    confidence_floor=float(self.cfg["runtime"]["needle_confidence_floor"]),
                    max_decisions=self.cfg["runtime"]["needle_max_decisions"],
                    max_tools=self.cfg["runtime"]["needle_max_tools"],
                    cancelled=self._needle_cancel.is_set)
                loop_outcome = loop.run(
                    {**response, "function_calls":calls}, permit_feedback=True)
                calls = list(loop_outcome.accepted_calls)
                # The loop itself executed these validated read-only calls.
                # Ingest its aligned post-execution evidence exactly once,
                # regardless of success, handoff, cancellation or failure.
                # Qwen's separate VerifiedRead path only covers NEW reads.
                self._observe_needle_prefix(task, text, loop_outcome)
                if (self.cfg["runtime"]["escalation_enabled"]
                        and loop_outcome.stop in ("uncertain", "repeated", "partial", "budget")
                        and "approval" not in loop_outcome.result):
                    # Previously completed Needle reads were observed above
                    # before any later handoff model is consulted.
                    completed = loop_outcome.result.get("results") or []
                    used_decisions=sum(1 for event in self.sessions.events(task.task_id)
                                       if isinstance(event, Decision)
                                       and event.engine == "needle3")
                    # max_plan_steps can be lower than Needle's independent
                    # native read budget. Already executed reads may not fit
                    # a version-1 handoff; do not hide/replay them in an error.
                    total_budget=min(self.cfg["runtime"]["max_plan_steps"], MAX_HANDOFF_TOOLS)
                    if len(completed)>total_budget:
                        self.sessions.append(task.task_id, Completion(outcome="failed", reason="exception"))
                        self.feedback.tell(json.dumps({
                            "status":"incomplete", "partial":True,
                            "halt_reason":"handoff_budget_exhausted",
                            "message":"Previously completed read-only checks are preserved. "
                                      "No Qwen continuation or repeated tool call was allowed.",
                            "results":completed,
                        },default=str)[:Truncation.RESULT * 3 // 2])
                        return
                    outcome=self._handoff(
                        task,text,"incomplete",calls=calls,results=completed,
                        allow_actions=False,used_decisions=used_decisions)
                    self._finish_handoff(task, outcome, text)
                    return
                if loop_outcome.stop in ("unsafe", "cancelled"):
                    # The successful read prefix belongs in the response and
                    # context, but the full task did NOT complete. Never turn
                    # an unsafe/cancelled follow-up into a Qwen action or a
                    # ledger Completion(completed, executed).
                    halted = dict(loop_outcome.result)
                    halted["status"] = "incomplete"
                    halted["halt_reason"] = loop_outcome.stop
                    halted["partial"] = bool(halted.get("results"))
                    self.sessions.append(task.task_id, Completion(
                        outcome="rejected" if loop_outcome.stop == "unsafe" else "failed",
                        reason="unsafe_route" if loop_outcome.stop == "unsafe" else "shutdown"))
                    self.feedback.tell(json.dumps(halted, default=str)[:Truncation.RESULT * 3 // 2])
                    return
                if (loop_outcome.stop not in ("completed", "protected", "partial")
                        and not loop_outcome.result.get("partial", False)):
                    # No successful observations: reject without pretending
                    # that a requested tool ran. A completed read-only prefix
                    # is reported below, even when a later call was denied.
                    self.sessions.append(task.task_id, Completion(
                        outcome="rejected",
                        reason="low_confidence" if loop_outcome.stop == "uncertain" else "unsafe_route"))
                    self.feedback.tell(loop_outcome.result["message"])
                    return
                result = loop_outcome.result
            if loop_outcome is not None:
                pass  # Already guarded by System2 and recorded within the loop.
            elif not calls:
                if self.cfg["runtime"]["escalation_enabled"]:
                    outcome=self._handoff(task,text,"unsupported",allow_actions=False)
                    self._finish_handoff(task, outcome, text)
                    return
                self.sessions.append(task.task_id,
                                     Escalation(source_model="needle3", target_model="qwen", reason="unsupported"))
                result=self.reason(text,allow_actions=False)
            elif len(calls)==1 and calls[0].get("name")=="delegate_to_reasoner":
                if self.cfg["runtime"]["escalation_enabled"]:
                    outcome=self._handoff(task,text,"delegated",allow_actions=True)
                    self._finish_handoff(task, outcome, text)
                    return
                self.sessions.append(task.task_id,
                                     Escalation(source_model="needle3", target_model="qwen", reason="delegated"))
                result=self.reason(text,allow_actions=True)
            else:
                result=self.system2.direct(calls)
            if "approval" in result:
                approval=result["approval"]
                self.sessions.append(task.task_id,
                                     ApprovalReference(approval_id=approval["approval_id"]))
                for call in calls:
                    if call.get("name") in SCHEMA_BY_NAME and call["name"] != "delegate_to_reasoner":
                        self.sessions.append(task.task_id, Observation(tool=call["name"], outcome="staged"))
                self.sessions.append(task.task_id,
                                     Completion(outcome="pending_approval", reason="needs_approval"))
                message=(f"Local approval required for {approval['actions']}. "
                         f"On the Jetson, run: alfred --approve {approval['approval_id']}")
                self.feedback.tell(message)
            else:
                # Never correlate Qwen results with Needle's routing proposal:
                # the real executed tool names come only from System2's
                # typed, post-execution read channel. Remove before printing
                # or returning context to either model.
                verified = result.pop("_verified_reads", ())
                trusted = (verified if isinstance(verified, tuple)
                           and all(type(v) is VerifiedRead for v in verified) else ())
                observed = result.get("results") or []
                if isinstance(observed, list):
                    if loop_outcome is None:
                        for item in trusted:
                            self.sessions.append(task.task_id, Observation(tool=item.tool, outcome="returned"))
                        if route == "direct" and not trusted:
                            # Compatibility path for pre-typed legacy/fake
                            # direct Needle results; never infer Qwen names.
                            for call, value in zip(calls, observed):
                                if (call.get("name") in SCHEMA_BY_NAME and
                                    call["name"] != "delegate_to_reasoner" and
                                    not (isinstance(value,dict) and value.get("skipped") is True)):
                                    self.sessions.append(task.task_id, Observation(tool=call["name"], outcome="returned"))
                    # Require locally returned read data and an explicit
                    # user-requested category; no model-authored observation.
                    for item in trusted:
                        if self.context.enabled:
                            self.context.observe(self.session_id,task.task_id,
                                                 routed_text,item.tool,item.output,
                                                 confidence=(safe_confidence or 0)
                                                 if route == "direct" else 1.0)
                        if (not self.context.enabled and
                                self.cfg["runtime"]["needle_loop_enabled"]
                                and item.tool == "get_gpu_status"):
                            self.needle_slots.remember(
                                self.session_id,routed_text,item.tool,item.output)
                    if (route == "direct" and not trusted
                            and (loop_outcome is None or not self.context.enabled)):
                        # Real Needle-loop read evidence was already ingested
                        # once above; legacy fallback applies only to
                        # one-shot/direct paths (or old SessionSlots mode).
                        for call, value in zip(calls, observed):
                            if (isinstance(value,dict) and value.get("skipped") is True):
                                continue
                            if self.context.enabled:
                                self.context.observe(self.session_id,task.task_id,
                                                     routed_text,call.get("name"),value,
                                                     confidence=safe_confidence or 0)
                            if (not self.context.enabled and
                                    self.cfg["runtime"]["needle_loop_enabled"]
                                    and call.get("name") == "get_gpu_status"):
                                self.needle_slots.remember(
                                    self.session_id,routed_text,"get_gpu_status",value)
                outcome = ("disabled" if not self.cfg["qwen"]["enabled"] and route != "direct"
                           else "no_action" if not observed else "executed")
                incomplete = result.get("status") == "incomplete"
                self.sessions.append(task.task_id, Completion(
                    outcome="failed" if incomplete or outcome == "disabled" else "completed",
                    reason=("invalid_plan" if result.get("error") == "invalid_plan"
                            else "exception") if incomplete else outcome,
                ))
                self.feedback.tell(json.dumps(result,default=str)[:Truncation.RESULT * 3 // 2])
        except Exception as exc:
            LOG.exception("Command failed")
            try:
                self.sessions.append(task.task_id, Completion(outcome="failed", reason="exception"))
            except Exception:
                LOG.exception("Could not record failed command")
            self.feedback.tell(f"Could not complete command: {exc}")

    def listen(self):
        if self.cactus is None:
            raise RuntimeError("Cactus models not loaded")
        wake_cfg = self.cfg["wake_word"]
        detector = (AcousticWakeDetector(wake_cfg)
                    if wake_cfg["enabled"] and wake_cfg["mode"] == "openwakeword"
                    else None)
        LOG.info("Starting voice capture; wake word %s (%s)",
                 "enabled" if wake_cfg["enabled"] else "disabled", wake_cfg["mode"])
        for chunks in UtteranceStream(self.audio.frames(),self.cfg["audio"]).utterances():
            transcript_display=TranscriptDisplay()
            try:
                acoustically_activated=False
                if detector is not None and not self.wake_word.is_armed():
                    # No expensive ASR inference before a local acoustic wake hit.
                    # A standalone wake also opens one short follow-up window.
                    chunks=detector.capture_after_wake(chunks,self.cfg["audio"]["frame_ms"])
                    if chunks is None:
                        continue
                    acoustically_activated=True

                def show_partial(hypothesis):
                    # Partial hypotheses are display-only, never executable.
                    # Suppress unrelated bystander speech from the terminal.
                    if self.wake_word.preview(
                            hypothesis, acoustic_activation=acoustically_activated) is not None:
                        transcript_display.update(hypothesis)

                self.admission.set_capture_active(True)
                try:
                    transcript=self.cactus.transcribe_stream(chunks,on_partial=show_partial)
                finally:
                    self.admission.set_capture_active(False)
                command=self.wake_word.consume(
                    transcript,acoustic_activation=acoustically_activated)
                if command is None:
                    transcript_display.discard()
                    continue
                transcript_display.finish(transcript or wake_cfg["phrase"])
                if command:
                    # This is a deterministic local command AFTER wake-word
                    # authorization; never send shutdown requests to Needle/Qwen.
                    if re.fullmatch(r"(?:close|exit|quit|stop listening|shut down|shutdown)[.!?]*",
                                    command.strip(), flags=re.IGNORECASE):
                        task = self._start_task("voice", len(command))
                        self.sessions.append(task.task_id, Decision(engine="local", route="shutdown", call_count=0))
                        self.sessions.append(task.task_id, Completion(outcome="completed", reason="shutdown"))
                        LOG.info("Wake-authorized spoken shutdown")
                        print("Stopping Alfred", flush=True)
                        break
                    LOG.info("Wake-authorized command received (%d characters)",len(command))
                    # Never forward a wake phrase to Needle or Qwen.
                    previous_source = self._command_source
                    self._command_source = "voice"
                    try:
                        self.handle_text(command)
                    finally:
                        self._command_source = previous_source
                else:
                    LOG.info("Wake word detected; awaiting follow-up command")
                # Discard speech buffered during synchronous TTS to prevent
                # the assistant from triggering itself.
                if self.feedback.tts:
                    self.audio.close()
            except (KeyboardInterrupt, AudioUnavailableError):
                transcript_display.abort()
                raise
            except Exception:
                transcript_display.abort()
                LOG.exception("Audio/transcription error, recovering")
                self.audio.close()
                if detector is not None:
                    detector.reset()
                time.sleep(1)

    def close(self):
        """Idempotent teardown; never terminate an inference server we adopted."""
        if self._closed:
            return
        self._closed = True
        self.admission.close()
        self._watchdog_stop.set()
        self._needle_cancel.set()
        self.needle_slots.clear()
        if self.context.enabled and self.session_id:
            self.context.clear_session(self.session_id)
        # Stop startup/restart attempts before waiting for the watchdog.
        for component, action in (
            ("audio capture", self.audio.close),
            ("harness subprocess", self.harness.close if self.harness else lambda: None),
            ("inference backend", self.supervisor.close),
            ("task and speech subprocesses", self.processes.close),
            ("scheduler", self.scheduler.stop),
        ):
            try:
                action()
            except Exception:
                LOG.exception("Could not fully stop %s", component)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=5)
        threads = (self.scheduler.thread, self.scheduler.workflow_thread,
                   self._watchdog_thread)
        if any(t is not None and t.is_alive() for t in threads):
            LOG.warning("Background workers did not all exit; leaving DB open until process exit")
        else:
            # Never surrender session ownership while another thread is
            # still inside a live model/tool command.
            if self._needle_task_lock.acquire(blocking=False):
                try:
                    self.sessions.release_ownership()
                finally:
                    self._needle_task_lock.release()
            else:
                LOG.warning("Active command still owns its session; retaining ownership")
            self.store.db.close()

# ---------------------------------------------------------------------------
# Backward-compatibility re-exports: doctor, _main, main moved to alfred.cli
# ---------------------------------------------------------------------------
from .cli import doctor, _main, main  # noqa: E402  (must follow Alfred class)


if __name__ == "__main__":
    raise SystemExit(main())
