"""Request routing and model orchestration — Needle dispatch and Qwen handoff."""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from typing import Any

from .constants import Limits, LoopOutcomeType, RouteType, Truncation
from .types import HandoffRequest, PlanSummary, ToolResult
from .cognitive import (
    ApprovalReference,
    Completion,
    Decision,
    Diagnostic,
    Escalation,
    Observation,
    TaskEnvelope,
)
from .cognitive.escalation import MAX_HANDOFF_TOOLS, build_handoff
from .cognitive.harness_sdk import HarnessUnavailable, LocalHarness
from .cognitive.needle_loop import (
    GPU_TEMP,
    MULTISTEP,
    PRONOUN,
    READONLY,
    ContextUnavailable,
    LoopOutcome,
    NeedleAgentLoop,
)
from .feedback import Feedback
from .inference_admission import InferenceAdmission
from .models import Cactus, LlamaSupervisor
from .runtime import SCHEMA_BY_NAME, TOOL_SCHEMAS, Store, Tools
from .session import SessionManager
from .system2 import HandoffConfig, System2, VerifiedRead, parse_plan

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded command patterns
# ---------------------------------------------------------------------------

# "Alfred minutes" / "start minutes" / "meeting minutes" — bypasses Needle and
# directly queues the approved `meeting_minutes` workflow.  The workflow must
# have been drafted and approved via `--workflow-draft` / `--workflow-approve`
# before this command will succeed.
_MINUTES = re.compile(
    r"^(?:start\s+)?(?:(?:the\s+)?meeting\s+)?minutes(?:\s+of\s+(?:the\s+)?meeting)?[.!?]*$",
    re.IGNORECASE,
)

MEETING_MINUTES_WORKFLOW = "meeting_minutes"

# ---------------------------------------------------------------------------
# GPU-status routing helpers
# ---------------------------------------------------------------------------

_GPU = re.compile(r"\b(?:gpu|graphics\s+(?:card|processor))\b", re.I)
_STATUS = re.compile(
    r"\b(?:status|temp(?:erature)?|usage|utili[sz]ation|load|health|"
    r"metric(?:s)?|memory|vram|frequency|clock|power)\b",
    re.I,
)
_OTHER_COMPONENT = re.compile(
    r"\b(?:cpu|system|overall|whole\s+device|ram|disk|storage)\b", re.I
)
_EXCLUSION = re.compile(
    r"\b(?:why|explain|compare|interpret|cause|reason|"
    r"don't|do\s+not|never|without|instead\s+of|"
    r"remind|reminder|schedule|timer|stop|start|restart|turn\s+off|delete)\b",
    re.I,
)


def explicit_gpu_status_request(utterance: str) -> bool:
    """True only for straightforward spoken GPU status/metrics requests."""
    return bool(
        isinstance(utterance, str)
        and _GPU.search(utterance)
        and _STATUS.search(utterance)
        and not _EXCLUSION.search(utterance)
    )


def adjust_status_calls(utterance: str, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copied tool-call list; change only Needle status-only decisions."""
    if not explicit_gpu_status_request(utterance):
        return calls
    if not isinstance(calls, list) or any(
        not isinstance(call, dict)
        or call.get("name") not in ("get_gpu_status", "get_system_status")
        for call in calls
    ):
        return calls
    if not calls:
        return [{"name": "get_gpu_status", "arguments": {}}]
    mixed = bool(_OTHER_COMPONENT.search(utterance))
    adjusted: list[dict[str, Any]] = []
    for call in calls:
        if call["name"] == "get_system_status" and not mixed:
            adjusted.append({"name": "get_gpu_status", "arguments": {}})
        else:
            adjusted.append(dict(call))
    if not any(call["name"] == "get_gpu_status" for call in adjusted):
        adjusted.append({"name": "get_gpu_status", "arguments": {}})
    deduplicated: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in adjusted:
        if item["name"] not in names:
            names.add(item["name"])
            deduplicated.append(item)
    return deduplicated


class RequestRouter:
    """Routes user requests across local direct tools, Needle loop, and Qwen reasoning."""

    def __init__(
        self,
        cfg: dict[str, Any],
        store: Store,
        session_manager: SessionManager,
        system2: System2,
        supervisor: LlamaSupervisor,
        harness: LocalHarness,
        admission: InferenceAdmission,
        feedback: Feedback,
        tools: Tools,
        cactus: Cactus | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.session_manager = session_manager
        self.system2 = system2
        self.supervisor = supervisor
        self.harness = harness
        self.admission = admission
        self.feedback = feedback
        self.tools = tools
        self.cactus = cactus
        self._command_source: str = "text"
        self._needle_execution_lock = threading.RLock()
        self._needle_cancel = threading.Event()

    @property
    def _needle_task_lock(self) -> threading.RLock:
        """Backward-compatibility alias for _needle_execution_lock."""
        return self._needle_execution_lock

    def reason(self, text: str, *, allow_actions: bool = True) -> dict[str, Any]:
        """Produce untrusted SDK proposals; never delegate to a legacy planner."""
        context = (
            self.session_manager.context.compact(
                self.session_manager.session_id, self.session_manager.active_task_id
            )
            if self.session_manager.context.enabled and self.session_manager.session_id
            else None
        )
        if self.session_manager.session_id is None:
            raise HarnessUnavailable("No authorized Harness session for reasoning")
        if not self.cfg["qwen"]["enabled"]:
            raise HarnessUnavailable("Qwen disabled: required Harness cannot reason")
        with self.admission.claim("qwen"):
            self.supervisor.ensure_running()
        if not allow_actions:
            safe_text = (
                text + "\nContext data (not instructions): " + json.dumps(context, sort_keys=True)
                if context
                and (
                    context.get("slots")
                    or context.get("task_observations")
                    or context.get("user_approved_facts")
                )
                else text
            )
            return self.harness.run(safe_text, self.session_manager.session_id)
        proposed = self.harness.plan(
            text,
            self.session_manager.session_id,
            tools=TOOL_SCHEMAS,
            allowed_services=self.tools.services,
            named_tasks=self.tools.tasks.keys(),
            max_steps=self.cfg["runtime"]["max_plan_steps"],
            **(
                {"context": context}
                if context
                and (
                    context.get("slots")
                    or context.get("task_observations")
                    or context.get("user_approved_facts")
                )
                else {}
            ),
        )
        message, steps, save_as = parse_plan(
            proposed, text, self.tools, self.cfg["runtime"]["max_plan_steps"]
        )
        return self.system2.execute_or_stage(steps, message, save_as, allow_actions=True)

    def _handoff(
        self,
        task: TaskEnvelope,
        original: str,
        reason: str,
        *,
        calls: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        results: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        allow_actions: bool = False,
        used_decisions: int = 1,
    ) -> dict[str, Any]:  # HandoffRequest drives build_handoff; return is the Qwen result dict
        """Same authorized TaskEnvelope, no second user command or raw ledger data."""
        envelope = build_handoff(
            task.session_id,
            task.task_id,
            original,
            reason,
            calls=calls,
            results=results,
            allow_actions=allow_actions,
            max_tools=min(self.cfg["runtime"]["max_plan_steps"], MAX_HANDOFF_TOOLS),
            needle_budget=self.cfg["runtime"]["needle_max_decisions"],
            used_decisions=used_decisions,
        )
        self.session_manager.sessions.append(
            task.task_id,
            Escalation(source_model="needle3", target_model="qwen", reason=reason),
        )
        self.session_manager.sessions.append(
            task.task_id,
            Decision(
                engine="qwen",
                route="delegate" if allow_actions else "readonly",
                call_count=0,
            ),
        )
        bounded_context = (
            self.session_manager.context.compact(task.session_id, task.task_id)
            if self.session_manager.context.enabled
            else None
        )
        return self.system2.continue_handoff(
            envelope,
            HandoffConfig(
                harness=self.harness,
                supervisor=self.supervisor,
                admission=self.admission,
                session_context=bounded_context,
            ),
        )

    def _observe_needle_prefix(
        self, task: TaskEnvelope, original: str, outcome: LoopOutcome
    ) -> None:
        """Preserve verified Needle-loop reads before an early handoff/stop."""
        if not self.session_manager.context.enabled or type(outcome) is not LoopOutcome:
            return
        calls = outcome.accepted_calls
        names = outcome.observed
        values = outcome.result.get("results")
        if (
            type(calls) is not tuple
            or type(names) is not tuple
            or type(values) is not list
            or not (len(calls) == len(names) == len(values))
        ):
            return
        for call, name, value in zip(calls, names, values):
            if (
                type(call) is not dict
                or type(name) is not str
                or call.get("name") != name
                or name not in READONLY
                or (type(value) is dict and value.get("skipped") is True)
            ):
                continue
            self.session_manager.context.observe(
                task.session_id, task.task_id, original, name, value, confidence=1.0
            )

    def _finish_handoff(
        self, task: TaskEnvelope, result: dict[str, Any], original: str
    ) -> None:
        """Consume typed handoff reads on the SAME task and finish safely."""
        verified = result.pop("_verified_reads", ())
        if (
            self.session_manager.context.enabled
            and "approval" not in result
            and result.get("status") != "incomplete"
            and type(verified) is tuple
        ):
            for item in verified:
                if (
                    type(item) is VerifiedRead
                    and not (isinstance(item.output, dict) and item.output.get("skipped") is True)
                ):
                    self.session_manager.context.observe(
                        task.session_id,
                        task.task_id,
                        original,
                        item.tool,
                        item.output,
                        confidence=1.0,
                    )
        if "approval" in result:
            proposal = result["approval"]
            self.session_manager.sessions.append(
                task.task_id,
                ApprovalReference(approval_id=proposal["approval_id"]),
            )
            for name in result.get("proposed_tools", ()):
                self.session_manager.sessions.append(
                    task.task_id, Observation(tool=name, outcome="staged")
                )
            self.session_manager.sessions.append(
                task.task_id,
                Completion(outcome="pending_approval", reason="needs_approval"),
            )
            self.feedback.tell(
                f"Local approval required for {proposal['actions']}. "
                f"On the Jetson, run: alfred --approve {proposal['approval_id']}"
            )
        elif result.get("status") == "incomplete":
            reason = (
                "invalid_plan"
                if result.get("error") == "invalid_plan"
                else "disabled"
                if not self.cfg["qwen"]["enabled"]
                else "exception"
            )
            self.session_manager.sessions.append(
                task.task_id, Completion(outcome="failed", reason=reason)
            )
            self.feedback.tell(json.dumps(result, default=str)[: Truncation.RESULT * 3 // 2])
        else:
            for item in result.get("tool_outcomes", ()):
                self.session_manager.sessions.append(
                    task.task_id,
                    Observation(tool=item["tool"], outcome=item["outcome"]),
                )
            self.session_manager.sessions.append(
                task.task_id,
                Completion(
                    outcome="completed",
                    reason="executed" if result.get("results") else "no_action",
                ),
            )
            self.feedback.tell(json.dumps(result, default=str)[: Truncation.RESULT * 3 // 2])

    def handle_text(self, text: str) -> None:
        """Entry point for processing typed or spoken text requests."""
        with self._needle_execution_lock:
            self._handle_text_locked(text)

    def _handle_text_locked(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        task = self.session_manager.start_task(self._command_source, len(text))
        self.session_manager.context.start_task(task.session_id, task.task_id)
        try:
            return self._process_authorized_task(task, text)
        finally:
            self.session_manager.context.finish_task(task.session_id, task.task_id)
            self.session_manager.clear_active_task()

    def _start_minutes_workflow(self, task: TaskEnvelope) -> None:
        """Hardcoded fast-path: queue the meeting_minutes workflow without Needle."""
        LOG.info("Hardcoded minutes command: starting %r workflow", MEETING_MINUTES_WORKFLOW)
        self.session_manager.sessions.append(
            task.task_id, Decision(engine="local", route="direct", call_count=1)
        )
        try:
            result = self.tools.workflow_engine.start(MEETING_MINUTES_WORKFLOW)
        except ValueError as exc:
            self.session_manager.sessions.append(
                task.task_id, Completion(outcome="failed", reason="exception")
            )
            self.feedback.tell(
                f"Could not start minutes workflow: {exc}. "
                "Run: alfred --workflow-draft minutes.json && alfred --workflow-approve meeting_minutes"
            )
            return
        self.session_manager.sessions.append(
            task.task_id,
            Observation(tool="run_workflow", outcome="returned"),
        )
        self.session_manager.sessions.append(
            task.task_id, Completion(outcome="completed", reason="executed")
        )
        self.store.event("workflow_queued", {"name": MEETING_MINUTES_WORKFLOW, "run_id": result.get("run_id")})
        self.feedback.tell(json.dumps(result, default=str))

    def _handle_fast_paths(self, task: TaskEnvelope, text: str) -> bool:
        """Handle hardcoded workflow shortcuts and session-level command directives."""
        if _MINUTES.fullmatch(text.strip()):
            self._start_minutes_workflow(task)
            return True
        if self.session_manager.context.enabled and re.fullmatch(
            r"(?:clear|forget) (?:this|current) session" r"(?: context)?[.!?]*", text, re.I
        ):
            self.session_manager.clear_session(task.session_id, task_id=task.task_id)
            self.session_manager.sessions.append(
                task.task_id, Decision(engine="local", route="direct", call_count=0)
            )
            self.session_manager.sessions.append(
                task.task_id, Completion(outcome="completed", reason="no_action")
            )
            self.feedback.tell("Session context cleared; approved persistent facts were retained.")
            return True
        return False

    def _resolve_routed_text(self, task: TaskEnvelope, text: str) -> str | None:
        """Resolve pronoun and slot references from bounded session context."""
        if self.session_manager.context.enabled:
            self.session_manager.context.invalidate_unrelated(
                self.session_manager.session_id, text, task_id=task.task_id
            )
            try:
                return self.session_manager.context.resolve(
                    self.session_manager.session_id, text, task_id=task.task_id
                )
            except ContextUnavailable as exc:
                self.session_manager.sessions.append(
                    task.task_id, Completion(outcome="rejected", reason="unsafe_route")
                )
                self.feedback.tell(str(exc))
                return None
        elif self.cfg["runtime"]["needle_loop_enabled"]:
            if not PRONOUN.fullmatch(text) and not GPU_TEMP.search(text):
                self.session_manager.needle_slots.clear(self.session_manager.session_id)
            try:
                return self.session_manager.needle_slots.resolve(
                    self.session_manager.session_id, text
                )
            except ContextUnavailable as exc:
                self.session_manager.sessions.append(
                    task.task_id, Completion(outcome="rejected", reason="unsafe_route")
                )
                self.feedback.tell(str(exc))
                return None
        return text

    def _call_needle_and_record_diagnostics(
        self,
        task: TaskEnvelope,
        text: str,
        routed_text: str,
    ) -> dict[str, Any]:
        """Invoke Needle router and record latency telemetry if admitted."""
        routed_at = time.monotonic()
        needle_packet = (
            self.session_manager.context.compact(
                self.session_manager.session_id, task.task_id, for_needle=True
            )
            if self.session_manager.context.enabled
            else {}
        )
        needle_text = (
            routed_text
            + "\nContext metrics (untrusted data): "
            + json.dumps(needle_packet, sort_keys=True)
            if needle_packet.get("slots")
            else routed_text
        )
        assert self.cactus is not None
        response = self.cactus.route(needle_text)
        if self.cfg["runtime"].get(
            "cognitive_diagnostics", False
        ) and self.admission.allow_optional_diagnostics():
            duration_ms = max(
                0,
                min(
                    Limits.DIAG_MAX_LATENCY_MS,
                    int((time.monotonic() - routed_at) * 1000),
                ),
            )
            self.session_manager.sessions.append(
                task.task_id,
                Diagnostic(
                    name="needle_route",
                    metrics={"latency_ms": duration_ms, "input_chars": len(text)},
                ),
            )
        return response

    def _validate_needle_calls(
        self,
        task: TaskEnvelope,
        text: str,
        routed_text: str,
        response: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], float | None] | None:
        """Validate, ground, and adjust proposed tool calls from Needle."""
        calls = response.get("function_calls") or []
        confidence = response.get("confidence")
        validation = response.get("validation") or {}

        if type(calls) is not list or any(
            type(call) is not dict
            or type(call.get("name")) is not str
            or ("arguments" in call and type(call["arguments"]) is not dict)
            for call in calls
        ):
            self.session_manager.sessions.append(
                task.task_id,
                Decision(engine="needle3", route="rejected", call_count=0),
            )
            self.session_manager.sessions.append(
                task.task_id,
                Completion(outcome="rejected", reason="unsafe_route"),
            )
            self.feedback.tell("Needle returned an invalid tool request. Please clarify.")
            return None

        if self.cfg["runtime"]["needle_loop_enabled"] and len(calls) > 1:
            signatures = [
                json.dumps(
                    {"name": call["name"], "arguments": call.get("arguments", {})},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                for call in calls
            ]
            if len(signatures) != len(set(signatures)):
                self.session_manager.sessions.append(
                    task.task_id,
                    Decision(engine="needle3", route="rejected", call_count=0),
                )
                self.session_manager.sessions.append(
                    task.task_id,
                    Completion(outcome="rejected", reason="unsafe_route"),
                )
                self.feedback.tell("Repeated tool request rejected before execution.")
                return None

        if response.get("suppressed_calls") or (
            isinstance(validation, dict) and validation.get("ungrounded")
        ):
            self.session_manager.sessions.append(
                task.task_id,
                Decision(
                    engine="needle3",
                    route="rejected",
                    call_count=min(len(calls), 50),
                ),
            )
            self.session_manager.sessions.append(
                task.task_id,
                Completion(outcome="rejected", reason="unsafe_route"),
            )
            self.feedback.tell("I couldn't safely determine that command. Please clarify.")
            return None

        floor = float(self.cfg["runtime"]["needle_confidence_floor"])
        if calls and (
            type(confidence) not in (float, int)
            or not math.isfinite(confidence)
            or not floor <= confidence <= 1
        ):
            self.store.event(
                "route_uncertain",
                {"confidence": confidence, "call_count": len(calls)},
            )
            self.session_manager.sessions.append(
                task.task_id,
                Decision(
                    engine="needle3",
                    route="uncertain",
                    call_count=min(len(calls), 50),
                ),
            )
            if self.cfg["runtime"]["escalation_enabled"]:
                outcome = self._handoff(task, text, "uncertain", allow_actions=False)
                self._finish_handoff(task, outcome, text)
                return None
            self.session_manager.sessions.append(
                task.task_id,
                Completion(outcome="rejected", reason="low_confidence"),
            )
            self.feedback.tell("Command uncertain. Please repeat or be more specific.")
            return None

        corrected = adjust_status_calls(routed_text, calls)
        if corrected is not calls:
            self.store.event(
                "routing_adjustment",
                {
                    "reason": "explicit_gpu_status",
                    "original": [call.get("name") for call in calls],
                    "selected": [call.get("name") for call in corrected],
                },
            )
            calls = corrected

        if self.cfg["runtime"]["escalation_enabled"] and any(
            call["name"] not in SCHEMA_BY_NAME for call in calls
        ):
            self.session_manager.sessions.append(
                task.task_id,
                Decision(
                    engine="needle3",
                    route="rejected",
                    call_count=min(len(calls), 50),
                ),
            )
            outcome = self._handoff(task, text, "unsupported", allow_actions=False)
            self._finish_handoff(task, outcome, text)
            return None

        if (
            self.cfg["runtime"]["needle_loop_enabled"]
            and len(calls) > self.cfg["runtime"]["needle_max_tools"]
        ):
            self.session_manager.sessions.append(
                task.task_id,
                Completion(outcome="rejected", reason="unsafe_route"),
            )
            self.feedback.tell("Needle exceeded the per-task tool budget. Please clarify.")
            return None

        return calls, confidence

    def _execute_multistep_loop(
        self,
        task: TaskEnvelope,
        text: str,
        routed_text: str,
        calls: list[dict[str, Any]],
        response: dict[str, Any],
        route: str,
        safe_confidence: float | None,
    ) -> None:
        """Run iterative Needle feedback loop with early handoff budget checking."""
        assert self.cactus is not None
        loop = NeedleAgentLoop(
            self.system2,
            self.cactus.route_feedback,
            self.session_manager.sessions,
            task.task_id,
            confidence_floor=float(self.cfg["runtime"]["needle_confidence_floor"]),
            max_decisions=self.cfg["runtime"]["needle_max_decisions"],
            max_tools=self.cfg["runtime"]["needle_max_tools"],
            cancelled=self._needle_cancel.is_set,
        )
        loop_outcome = loop.run({**response, "function_calls": calls}, permit_feedback=True)
        accepted_calls = list(loop_outcome.accepted_calls)
        self._observe_needle_prefix(task, text, loop_outcome)

        if (
            self.cfg["runtime"]["escalation_enabled"]
            and loop_outcome.stop in ("uncertain", "repeated", "partial", "budget")
            and "approval" not in loop_outcome.result
        ):
            completed = loop_outcome.result.get("results") or []
            used_decisions = sum(
                1
                for event in self.session_manager.sessions.events(task.task_id)
                if isinstance(event, Decision) and event.engine == "needle3"
            )
            total_budget = min(self.cfg["runtime"]["max_plan_steps"], MAX_HANDOFF_TOOLS)
            if len(completed) > total_budget:
                self.session_manager.sessions.append(
                    task.task_id,
                    Completion(outcome="failed", reason="exception"),
                )
                self.feedback.tell(
                    json.dumps(
                        {
                            "status": "incomplete",
                            "partial": True,
                            "halt_reason": "handoff_budget_exhausted",
                            "message": (
                                "Previously completed read-only checks are preserved. "
                                "No Qwen continuation or repeated tool call was allowed."
                            ),
                            "results": completed,
                        },
                        default=str,
                    )[: Truncation.RESULT * 3 // 2]
                )
                return
            outcome = self._handoff(
                task,
                text,
                "incomplete",
                calls=accepted_calls,
                results=completed,
                allow_actions=False,
                used_decisions=used_decisions,
            )
            self._finish_handoff(task, outcome, text)
            return

        if loop_outcome.stop in ("unsafe", "cancelled"):
            halted = dict(loop_outcome.result)
            halted["status"] = "incomplete"
            halted["halt_reason"] = loop_outcome.stop
            halted["partial"] = bool(halted.get("results"))
            self.session_manager.sessions.append(
                task.task_id,
                Completion(
                    outcome="rejected" if loop_outcome.stop == "unsafe" else "failed",
                    reason="unsafe_route" if loop_outcome.stop == "unsafe" else "shutdown",
                ),
            )
            self.feedback.tell(json.dumps(halted, default=str)[: Truncation.RESULT * 3 // 2])
            return

        if (
            loop_outcome.stop not in ("completed", "protected", "partial")
            and not loop_outcome.result.get("partial", False)
        ):
            self.session_manager.sessions.append(
                task.task_id,
                Completion(
                    outcome="rejected",
                    reason="low_confidence" if loop_outcome.stop == "uncertain" else "unsafe_route",
                ),
            )
            self.feedback.tell(loop_outcome.result["message"])
            return

        self._finalize_task_result(
            task,
            text,
            routed_text,
            loop_outcome.result,
            accepted_calls,
            route,
            loop_outcome,
            safe_confidence,
        )

    def _execute_direct_or_reason(
        self,
        task: TaskEnvelope,
        text: str,
        calls: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Execute single-turn direct tool calls or delegate to System 2 reasoner."""
        if not calls:
            if self.cfg["runtime"]["escalation_enabled"]:
                outcome = self._handoff(task, text, "unsupported", allow_actions=False)
                self._finish_handoff(task, outcome, text)
                return None
            self.session_manager.sessions.append(
                task.task_id,
                Escalation(source_model="needle3", target_model="qwen", reason="unsupported"),
            )
            return self.reason(text, allow_actions=False)

        if len(calls) == 1 and calls[0].get("name") == "delegate_to_reasoner":
            if self.cfg["runtime"]["escalation_enabled"]:
                outcome = self._handoff(task, text, "delegated", allow_actions=True)
                self._finish_handoff(task, outcome, text)
                return None
            self.session_manager.sessions.append(
                task.task_id,
                Escalation(source_model="needle3", target_model="qwen", reason="delegated"),
            )
            return self.reason(text, allow_actions=True)

        return self.system2.direct(calls)

    def _finalize_task_result(
        self,
        task: TaskEnvelope,
        text: str,
        routed_text: str,
        result: dict[str, Any],
        calls: list[dict[str, Any]],
        route: str,
        loop_outcome: LoopOutcome | None,
        safe_confidence: float | None,
    ) -> None:
        """Stage approvals or record execution observations and final task completion."""
        if "approval" in result:
            approval = result["approval"]
            self.session_manager.sessions.append(
                task.task_id,
                ApprovalReference(approval_id=approval["approval_id"]),
            )
            for call in calls:
                if call.get("name") in SCHEMA_BY_NAME and call["name"] != "delegate_to_reasoner":
                    self.session_manager.sessions.append(
                        task.task_id,
                        Observation(tool=call["name"], outcome="staged"),
                    )
            self.session_manager.sessions.append(
                task.task_id,
                Completion(outcome="pending_approval", reason="needs_approval"),
            )
            message = (
                f"Local approval required for {approval['actions']}. "
                f"On the Jetson, run: alfred --approve {approval['approval_id']}"
            )
            self.feedback.tell(message)
            return

        verified = result.pop("_verified_reads", ())
        trusted = (
            verified
            if isinstance(verified, tuple) and all(type(v) is VerifiedRead for v in verified)
            else ()
        )
        observed = result.get("results") or []
        if isinstance(observed, list):
            self._record_observations_and_memory(
                task,
                routed_text,
                calls,
                observed,
                trusted,
                route,
                loop_outcome,
                safe_confidence,
            )

        outcome = (
            "disabled"
            if not self.cfg["qwen"]["enabled"] and route != "direct"
            else "no_action"
            if not observed
            else "executed"
        )
        incomplete = result.get("status") == "incomplete"
        self.session_manager.sessions.append(
            task.task_id,
            Completion(
                outcome="failed" if incomplete or outcome == "disabled" else "completed",
                reason=(
                    "invalid_plan" if result.get("error") == "invalid_plan" else "exception"
                )
                if incomplete
                else outcome,
            ),
        )
        self.feedback.tell(json.dumps(result, default=str)[: Truncation.RESULT * 3 // 2])

    def _record_observations_and_memory(
        self,
        task: TaskEnvelope,
        routed_text: str,
        calls: list[dict[str, Any]],
        observed: list[Any],
        trusted: tuple[VerifiedRead, ...],
        route: str,
        loop_outcome: LoopOutcome | None,
        safe_confidence: float | None,
    ) -> None:
        """Persist structured observations to session ledger and short-term slot memory."""
        if loop_outcome is None:
            for item in trusted:
                self.session_manager.sessions.append(
                    task.task_id,
                    Observation(tool=item.tool, outcome="returned"),
                )
            if route == "direct" and not trusted:
                for call, value in zip(calls, observed):
                    if (
                        call.get("name") in SCHEMA_BY_NAME
                        and call["name"] != "delegate_to_reasoner"
                        and not (isinstance(value, dict) and value.get("skipped") is True)
                    ):
                        self.session_manager.sessions.append(
                            task.task_id,
                            Observation(tool=call["name"], outcome="returned"),
                        )

        for item in trusted:
            if self.session_manager.context.enabled:
                self.session_manager.context.observe(
                    self.session_manager.session_id,
                    task.task_id,
                    routed_text,
                    item.tool,
                    item.output,
                    confidence=(safe_confidence or 0) if route == "direct" else 1.0,
                )
            if (
                not self.session_manager.context.enabled
                and self.cfg["runtime"]["needle_loop_enabled"]
                and item.tool == "get_gpu_status"
            ):
                self.session_manager.needle_slots.remember(
                    self.session_manager.session_id,
                    routed_text,
                    item.tool,
                    item.output,
                )

        if (
            route == "direct"
            and not trusted
            and (loop_outcome is None or not self.session_manager.context.enabled)
        ):
            for call, value in zip(calls, observed):
                if isinstance(value, dict) and value.get("skipped") is True:
                    continue
                if self.session_manager.context.enabled:
                    self.session_manager.context.observe(
                        self.session_manager.session_id,
                        task.task_id,
                        routed_text,
                        call.get("name"),
                        value,
                        confidence=safe_confidence or 0,
                    )
                if (
                    not self.session_manager.context.enabled
                    and self.cfg["runtime"]["needle_loop_enabled"]
                    and call.get("name") == "get_gpu_status"
                ):
                    self.session_manager.needle_slots.remember(
                        self.session_manager.session_id,
                        routed_text,
                        "get_gpu_status",
                        value,
                    )

    def _process_authorized_task(self, task: TaskEnvelope, text: str) -> None:  # noqa: C901
        """Coordinate execution of an authorized user task through routing and validation."""
        self.store.event("utterance", {"length": len(text)})
        if self._handle_fast_paths(task, text):
            return

        routed_text = self._resolve_routed_text(task, text)
        if routed_text is None:
            return

        if self.cactus is None:
            self.session_manager.sessions.append(
                task.task_id, Completion(outcome="failed", reason="exception")
            )
            raise RuntimeError("Cactus models were not initialized")

        try:
            response = self._call_needle_and_record_diagnostics(task, text, routed_text)
            validation_result = self._validate_needle_calls(task, text, routed_text, response)
            if validation_result is None:
                return
            calls, confidence = validation_result

            self.store.event(
                "route",
                {
                    "model": "needle3",
                    "confidence": confidence,
                    "call_count": len(calls),
                    "delegated": bool(
                        not calls
                        or (len(calls) == 1 and calls[0].get("name") == "delegate_to_reasoner")
                    ),
                },
            )

            route = (
                "readonly"
                if not calls
                else "delegate"
                if len(calls) == 1 and calls[0].get("name") == "delegate_to_reasoner"
                else "direct"
            )
            safe_confidence = (
                float(confidence)
                if type(confidence) in (float, int) and 0 <= confidence <= 1
                else None
            )
            self.session_manager.sessions.append(
                task.task_id,
                Decision(
                    engine="needle3",
                    route=route,
                    call_count=min(len(calls), 50),
                    confidence=safe_confidence,
                ),
            )

            if (
                self.cfg["runtime"]["needle_loop_enabled"]
                and calls
                and route == "direct"
                and MULTISTEP.search(text)
            ):
                self._execute_multistep_loop(
                    task, text, routed_text, calls, response, route, safe_confidence
                )
                return

            result = self._execute_direct_or_reason(task, text, calls)
            if result is None:
                return

            self._finalize_task_result(
                task, text, routed_text, result, calls, route, None, safe_confidence
            )
        except Exception as execution_error:
            LOG.exception("Command failed: %s", execution_error)
            try:
                self.session_manager.sessions.append(
                    task.task_id, Completion(outcome="failed", reason="exception")
                )
            except Exception as record_error:
                LOG.exception("Could not record failed command: %s", record_error)
            self.feedback.tell(f"Could not complete command: {execution_error}")
