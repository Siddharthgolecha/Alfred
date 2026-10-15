"""Alfred CLI and always-on voice orchestrator."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import logging
import math
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time

from .audio import AudioManager, AudioUnavailableError, UtteranceStream, pactl_json
from .console import TranscriptDisplay
from .routing import adjust_status_calls
from .config import data_path, load_config
from .models import Cactus, LlamaSupervisor, Qwen
from .lifecycle import ChildProcesses, ShutdownSocket, request_close
from .runtime import SCHEMA_BY_NAME, TOOL_SCHEMAS, Scheduler, Store, Tools
from .system2 import System2, parse_plan
from .wakeword import WakeWordGate, AcousticWakeDetector
from .cognitive import (SessionLedger, Decision, Observation, Escalation,
                        ApprovalReference, Completion, Diagnostic)
from .cognitive.harness_sdk import LocalHarness, HarnessUnavailable
from .cognitive.escalation import build_handoff, MAX_HANDOFF_TOOLS
from .cognitive.needle_loop import (NeedleAgentLoop, SessionSlots,
                                   ContextUnavailable, MULTISTEP)

LOG = logging.getLogger(__name__)

class Feedback:
    def __init__(self,enable_tts=False,processes=None):
        self.tts = enable_tts
        self.processes = processes
        self.speaking = threading.Event()

    def tell(self,message):
        print(message,flush=True)
        if self.tts and shutil.which("spd-say"):
            self.speaking.set()
            try:
                runner = self.processes.run if self.processes else subprocess.run
                runner(["spd-say","-w",str(message)[:300]],timeout=25,check=False)
            except (OSError,subprocess.TimeoutExpired) as exc:
                LOG.warning("Speech output failed: %s",exc)
            finally:
                self.speaking.clear()

class Alfred:
    def __init__(self,cfg,load_models=True):
        self.cfg = cfg
        runtime = cfg["runtime"]
        self.store = Store(runtime["database"])
        self.sessions = SessionLedger(self.store)
        self.session_id = None
        self._command_source = "text"
        self._needle_task_lock = threading.RLock()
        self.needle_slots = SessionSlots(runtime["needle_context_ttl_seconds"])
        self._needle_cancel = threading.Event()
        self.processes = ChildProcesses()
        self.tools = Tools(runtime,self.store,processes=self.processes)
        self.feedback = Feedback(runtime["tts"],processes=self.processes)
        self.audio = AudioManager(cfg["audio"])
        self.wake_word = WakeWordGate(cfg["wake_word"])
        self.supervisor = LlamaSupervisor(cfg["qwen"],runtime["logs"])
        self.qwen = Qwen(cfg["qwen"],self.supervisor)
        self.system2 = System2(self.qwen,self.tools,self.store,runtime)
        # Optional read-only Harness route, never a new tool authority/server.
        self.harness = None
        if runtime.get("harness_enabled", False):
            model = cfg["qwen"]["model"] or cfg["qwen"]["expected_model"]
            try:
                self.harness = LocalHarness(
                    self.supervisor.url if hasattr(self.supervisor, "url") else cfg["qwen"]["url"],
                    model, timeout=runtime["harness_timeout_seconds"])
            except HarnessUnavailable:
                LOG.warning("Safe harness unavailable; using legacy Qwen path")
        if not runtime.get("telemetry",False):
            import os
            os.environ.setdefault("NEEDLE_TELEMETRY","0")
        self.cactus = Cactus(TOOL_SCHEMAS) if load_models else None
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
                        self.supervisor.ensure_running()
                        LOG.info("Qwen available: %s", self.supervisor.status())
                    except Exception as exc:
                        LOG.warning("Qwen unavailable; will retry: %s", exc)
                    self._watchdog_stop.wait(int(self.cfg["qwen"]["supervisor_poll_seconds"]))
            self._watchdog_thread = threading.Thread(
                target=watchdog, name="model-supervisor", daemon=True)
            self._watchdog_thread.start()

    def reason(self, text, allow_actions=True):
        if not self.cfg["qwen"]["enabled"]:
            return {"message":"Qwen System 2 is disabled","results":[]}
        # The Harness only proposes; Python System2 remains the sole execution
        # and local approval authority. A failed SDK inference may safely fall
        # back BEFORE any tool is handed to System2. Never wrap execution in
        # that fallback handler: a partially completed action is not retryable.
        if self.harness is not None and self.session_id is not None:
            try:
                self.supervisor.ensure_running()  # existing model service only
                if not allow_actions:
                    return self.harness.run(text, self.session_id)
                proposed = self.harness.plan(
                    text, self.session_id, tools=TOOL_SCHEMAS,
                    allowed_services=self.tools.services,
                    named_tasks=self.tools.tasks.keys(),
                    max_steps=self.cfg["runtime"]["max_plan_steps"])
            except (HarnessUnavailable, OSError, TimeoutError, RuntimeError):
                LOG.warning("Harness inference unavailable before execution; falling back to System2")
                return self.system2.reason(text, allow_actions=allow_actions)
            # Invalid, spoofed or malformed SDK plans are a hard rejection,
            # not a second chance to reinterpret their action-bearing text.
            message, steps, save_as = parse_plan(
                proposed, text, self.tools,
                self.cfg["runtime"]["max_plan_steps"])
            # Deliberately OUTSIDE the retry block. If a tool runs and then
            # raises, the caller reports failure without a second execution.
            return self.system2.execute_or_stage(
                steps, message, save_as, allow_actions=True)
        return self.system2.reason(text,allow_actions=allow_actions)

    def _handoff(self, task, original, reason, *, calls=(), results=(),
                 allow_actions=False, used_decisions=1):
        """Same authorized TaskEnvelope, no second user command or raw ledger data."""
        envelope = build_handoff(
            task.session_id, task.task_id, original, reason,
            calls=calls, results=results, allow_actions=allow_actions,
            max_tools=min(self.cfg["runtime"]["max_plan_steps"], MAX_HANDOFF_TOOLS),
            needle_budget=self.cfg["runtime"]["needle_max_decisions"],
            used_decisions=used_decisions)
        self.sessions.append(task.task_id, Escalation("needle3", "qwen", reason))
        self.sessions.append(task.task_id, Decision(
            "qwen", "delegate" if allow_actions else "readonly", 0))
        return self.system2.continue_handoff(
            envelope, harness=self.harness, supervisor=self.supervisor)

    def _finish_handoff(self, task, result):
        """Apply the existing approval state machine to the SAME task."""
        if "approval" in result:
            proposal=result["approval"]
            self.sessions.append(task.task_id,
                                 ApprovalReference(proposal["approval_id"]))
            for name in result.get("proposed_tools", ()):
                self.sessions.append(task.task_id, Observation(name, "staged"))
            self.sessions.append(task.task_id,
                                 Completion("pending_approval", "needs_approval"))
            self.feedback.tell(
                f"Local approval required for {proposal['actions']}. "
                f"On the Jetson, run: alfred --approve {proposal['approval_id']}")
        elif result.get("status") == "incomplete":
            # An unavailable model does not transform a partial observation
            # into success. Previously executed tools remain visible.
            self.sessions.append(task.task_id, Completion("failed", "disabled"
                if not self.cfg["qwen"]["enabled"] else "exception"))
            self.feedback.tell(json.dumps(result,default=str)[:1800])
        else:
            for item in result.get("tool_outcomes", ()):
                self.sessions.append(task.task_id, Observation(
                    item["tool"], item["outcome"]))
            self.sessions.append(task.task_id, Completion(
                "completed", "executed" if result.get("results") else "no_action"))
            self.feedback.tell(json.dumps(result,default=str)[:1800])

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
        self.store.event("utterance",{"length":len(text)})  # No raw transcript stored.
        # A later unrelated command invalidates the only supported pronoun
        # referent. Context must never leak across topics or sessions.
        from .cognitive.needle_loop import PRONOUN, GPU_TEMP
        routed_text = text
        if self.cfg["runtime"]["needle_loop_enabled"]:
            if not PRONOUN.fullmatch(text) and not GPU_TEMP.search(text):
                self.needle_slots.clear(self.session_id)
            try:
                routed_text = self.needle_slots.resolve(self.session_id, text)
            except ContextUnavailable as exc:
                self.sessions.append(task.task_id, Completion("rejected", "unsafe_route"))
                self.feedback.tell(str(exc))
                return
        if self.cactus is None:
            self.sessions.append(task.task_id, Completion("failed", "exception"))
            raise RuntimeError("Cactus models were not initialized")
        try:
            routed_at = time.monotonic()
            response=self.cactus.route(routed_text)
            if self.cfg["runtime"].get("cognitive_diagnostics",False):
                duration = max(0, min(300000, int((time.monotonic() - routed_at)*1000)))
                self.sessions.append(task.task_id, Diagnostic("needle_route", {
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
                self.sessions.append(task.task_id, Decision("needle3", "rejected", 0))
                self.sessions.append(task.task_id, Completion("rejected", "unsafe_route"))
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
                    self.sessions.append(task.task_id, Decision("needle3", "rejected", 0))
                    self.sessions.append(task.task_id, Completion("rejected", "unsafe_route"))
                    self.feedback.tell("Repeated tool request rejected before execution.")
                    return
            if response.get("suppressed_calls") or (
                isinstance(validation, dict) and validation.get("ungrounded")
            ):
                self.sessions.append(task.task_id, Decision("needle3", "rejected",
                                     min(len(calls), 50)))
                self.sessions.append(task.task_id, Completion("rejected", "unsafe_route"))
                self.feedback.tell("I couldn't safely determine that command. Please clarify.")
                return
            floor=float(self.cfg["runtime"]["needle_confidence_floor"])
            if calls and (type(confidence) not in (float, int)
                          or not math.isfinite(confidence) or not floor <= confidence <= 1):
                self.store.event("route_uncertain",{
                    "confidence": confidence, "call_count": len(calls)})
                self.sessions.append(task.task_id, Decision("needle3", "uncertain",
                                     min(len(calls), 50)))
                if self.cfg["runtime"]["escalation_enabled"]:
                    outcome=self._handoff(task, text, "uncertain",allow_actions=False)
                    self._finish_handoff(task, outcome)
                    return
                self.sessions.append(task.task_id, Completion("rejected", "low_confidence"))
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
                self.sessions.append(task.task_id, Decision("needle3", "rejected",
                                                             min(len(calls), 50)))
                outcome=self._handoff(task, text, "unsupported", allow_actions=False)
                self._finish_handoff(task, outcome)
                return
            self.store.event("route", {
                "model":"needle3", "confidence":confidence,
                "call_count":len(calls),
                "delegated":bool(not calls or (len(calls)==1 and
                    calls[0].get("name")=="delegate_to_reasoner"))
            })
            if (self.cfg["runtime"]["needle_loop_enabled"]
                    and len(calls) > self.cfg["runtime"]["needle_max_tools"]):
                self.sessions.append(task.task_id, Completion("rejected", "unsafe_route"))
                self.feedback.tell("Needle exceeded the per-task tool budget. Please clarify.")
                return
            route = ("readonly" if not calls else
                     "delegate" if len(calls)==1 and
                     calls[0].get("name")=="delegate_to_reasoner" else "direct")
            safe_confidence = (float(confidence) if type(confidence) in (float,int)
                               and 0 <= confidence <= 1 else None)
            self.sessions.append(task.task_id, Decision("needle3", route,
                                 min(len(calls), 50), safe_confidence))
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
                if (self.cfg["runtime"]["escalation_enabled"]
                        and loop_outcome.stop in ("uncertain", "repeated", "partial", "budget")
                        and "approval" not in loop_outcome.result):
                    completed = loop_outcome.result.get("results") or []
                    used_decisions=sum(1 for event in self.sessions.events(task.task_id)
                                       if isinstance(event, Decision)
                                       and event.engine == "needle3")
                    # max_plan_steps can be lower than Needle's independent
                    # native read budget. Already executed reads may not fit
                    # a version-1 handoff; do not hide/replay them in an error.
                    total_budget=min(self.cfg["runtime"]["max_plan_steps"], MAX_HANDOFF_TOOLS)
                    if len(completed)>total_budget:
                        self.sessions.append(task.task_id, Completion("failed", "exception"))
                        self.feedback.tell(json.dumps({
                            "status":"incomplete", "partial":True,
                            "halt_reason":"handoff_budget_exhausted",
                            "message":"Previously completed read-only checks are preserved. "
                                      "No Qwen continuation or repeated tool call was allowed.",
                            "results":completed,
                        },default=str)[:1800])
                        return
                    outcome=self._handoff(
                        task,text,"incomplete",calls=calls,results=completed,
                        allow_actions=False,used_decisions=used_decisions)
                    self._finish_handoff(task,outcome)
                    return
                if (loop_outcome.stop not in ("completed", "protected", "partial")
                        and not loop_outcome.result.get("partial", False)):
                    # No successful observations: reject without pretending
                    # that a requested tool ran. A completed read-only prefix
                    # is reported below, even when a later call was denied.
                    self.sessions.append(task.task_id, Completion(
                        "rejected", "low_confidence" if loop_outcome.stop == "uncertain"
                        else "unsafe_route"))
                    self.feedback.tell(loop_outcome.result["message"])
                    return
                result = loop_outcome.result
            if loop_outcome is not None:
                pass  # Already guarded by System2 and recorded within the loop.
            elif not calls:
                if self.cfg["runtime"]["escalation_enabled"]:
                    outcome=self._handoff(task,text,"unsupported",allow_actions=False)
                    self._finish_handoff(task,outcome)
                    return
                self.sessions.append(task.task_id,
                                     Escalation("needle3", "qwen", "unsupported"))
                result=self.reason(text,allow_actions=False)
            elif len(calls)==1 and calls[0].get("name")=="delegate_to_reasoner":
                if self.cfg["runtime"]["escalation_enabled"]:
                    outcome=self._handoff(task,text,"delegated",allow_actions=True)
                    self._finish_handoff(task,outcome)
                    return
                self.sessions.append(task.task_id,
                                     Escalation("needle3", "qwen", "delegated"))
                result=self.reason(text,allow_actions=True)
            else:
                result=self.system2.direct(calls)
            if "approval" in result:
                approval=result["approval"]
                self.sessions.append(task.task_id,
                                     ApprovalReference(approval["approval_id"]))
                for call in calls:
                    if call.get("name") in SCHEMA_BY_NAME and call["name"] != "delegate_to_reasoner":
                        self.sessions.append(task.task_id, Observation(call["name"], "staged"))
                self.sessions.append(task.task_id,
                                     Completion("pending_approval", "needs_approval"))
                message=(f"Local approval required for {approval['actions']}. "
                         f"On the Jetson, run: alfred --approve {approval['approval_id']}")
                self.feedback.tell(message)
            else:
                # Persist only tool names/outcomes, never arguments or results.
                observed = result.get("results") or []
                if isinstance(observed, list):
                    if loop_outcome is None:
                        for call in calls[:len(observed)]:
                            if call.get("name") in SCHEMA_BY_NAME and call["name"] != "delegate_to_reasoner":
                                self.sessions.append(task.task_id, Observation(call["name"], "returned"))
                    # Never use model text to construct a context slot. Only
                    # a genuine, explicit successful GPU temperature read
                    # from the controlled System2 path refreshes that referent.
                    for call, item in zip(calls, observed):
                        if (self.cfg["runtime"]["needle_loop_enabled"]
                                and call.get("name") == "get_gpu_status"):
                            self.needle_slots.remember(
                                self.session_id, routed_text, "get_gpu_status", item)
                outcome = ("disabled" if not self.cfg["qwen"]["enabled"] and route != "direct"
                           else "no_action" if not observed else "executed")
                incomplete = result.get("status") == "incomplete"
                self.sessions.append(task.task_id,
                                     Completion("failed" if incomplete or outcome == "disabled"
                                                else "completed",
                                                ("invalid_plan" if result.get("error")=="invalid_plan"
                                                 else "exception") if incomplete else outcome))
                self.feedback.tell(json.dumps(result,default=str)[:1800])
        except Exception as exc:
            LOG.exception("Command failed")
            try:
                self.sessions.append(task.task_id, Completion("failed", "exception"))
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

                transcript=self.cactus.transcribe_stream(chunks,on_partial=show_partial)
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
                        self.sessions.append(task.task_id, Decision("local", "shutdown", 0))
                        self.sessions.append(task.task_id, Completion("completed", "shutdown"))
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
        self._watchdog_stop.set()
        self._needle_cancel.set()
        self.needle_slots.clear()
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

def doctor(cfg):
    """Make advertised devices and *verified live microphone* visibly distinct."""
    print("Audio input mode:",cfg["audio"]["mode"])
    manager=AudioManager(cfg["audio"])
    healthy=False
    try:
        entries=manager.diagnostics()
        if not entries:
            print("Audio server lists no recording sources.")
        for source in entries:
            tag="CANDIDATE" if source["eligible"] else "REJECTED"
            print(f"[{tag}] {source['description'] or source['name']}: "
                  f"{source['name']} — {source['reason']}")
        try:
            selected=manager.ensure_input()
            print(f"MICROPHONE INPUT STREAM VERIFIED: {selected}")
            healthy=True
        except AudioUnavailableError as exc:
            print(f"MICROPHONE NOT CONNECTED / NOT USABLE: {exc}")
    except Exception as exc:
        print(f"MICROPHONE NOT CONNECTED / AUDIO SERVER FAILURE: {exc}")
    finally:
        manager.close()
    try:
        for card in pactl_json("cards"):
            if str(card.get("name","")).startswith("bluez_card"):
                print("Bluetooth card:",card.get("name"),"profile:",card.get("active_profile"))
    except (OSError,ValueError,RuntimeError) as exc:
        print("Bluetooth profile detection unavailable:",exc)
    s=LlamaSupervisor(cfg["qwen"],cfg["runtime"]["logs"])
    print("Inference backend:",json.dumps(s.status()))
    print("Model:",cfg["qwen"]["model"] or cfg["qwen"]["model_path"] or "(auto-select)")
    print("Backend executable:",s.executable_name())
    return healthy

def _main(argv=None):
    parser=argparse.ArgumentParser(description="Alfred Jetson voice automation agent")
    parser.add_argument("--config",default="config.json",help="Local JSON config")
    parser.add_argument("--resume-session", metavar="SESSION_ID",
                        help="Explicitly reuse a prior local session ID; never replay interrupted tools")
    parser.add_argument("command", nargs="?", choices=["close"],
                        help="Close running Alfred services and processes owned by Alfred")
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--doctor",action="store_true",help="Show mic/server status without models")
    group.add_argument("--text",help="Execute one command using Needle and tools")
    group.add_argument("--ensure-backend","--ensure-llama",dest="ensure_backend",
                       action="store_true",help="Start/verify the selected inference backend then exit")
    group.add_argument("--scheduler-only",action="store_true",
                       help="Run reminders and watches without microphone, Cactus, or Qwen")
    group.add_argument("--automations",action="store_true",
                       help="List Phase 3 automations without loading native models")
    group.add_argument("--automation-tick",action="store_true",
                       help="Run one scheduler cycle for debugging and exit")
    group.add_argument("--reason",help="Test Qwen planner on a text input; no Cactus engine required")
    group.add_argument("--pending",action="store_true",help="List proposed tool actions awaiting approval")
    group.add_argument("--approve",type=int,metavar="ID",help="Locally approve and execute a proposal once")
    group.add_argument("--reject",type=int,metavar="ID",help="Locally reject a proposal")
    group.add_argument("--workflow-draft",metavar="JSON_FILE",
                       help="Validate and save a local workflow graph as unapproved draft")
    group.add_argument("--workflow-approve",metavar="NAME",
                       help="Locally approve a validated graph version")
    group.add_argument("--workflow-list",action="store_true",
                       help="List graph definitions and their approval state")
    group.add_argument("--workflow-start",metavar="NAME",
                       help="Locally queue an approved graph workflow")
    group.add_argument("--workflow-runs",action="store_true",
                       help="List recent workflow execution statuses")
    group.add_argument("--workflow-status",type=int,metavar="RUN_ID",
                       help="Inspect durable step results for a workflow run")
    group.add_argument("--workflow-cancel",type=int,metavar="RUN_ID",
                       help="Cancel pending steps of an in-progress graph")
    group.add_argument("--workflow-tick",action="store_true",
                       help="Execute up to three ready graph workflow steps")
    group.add_argument("--workflow-reconcile",type=int,metavar="RUN_ID",
                       help="Explicitly mark an uncertain in-flight step interrupted")
    group.add_argument("--workflow-schedule",metavar="NAME:MINUTES",
                       help="Schedule a locally approved read-only graph on an interval")
    group.add_argument("--workflow-triggers",action="store_true",
                       help="List recurring read-only workflow triggers")
    group.add_argument("--workflow-trigger-pause",type=int,metavar="TRIGGER_ID")
    group.add_argument("--workflow-trigger-resume",type=int,metavar="TRIGGER_ID")
    group.add_argument("--workflow-trigger-cancel",type=int,metavar="TRIGGER_ID")

    args=parser.parse_args(argv)
    path=Path(args.config)
    cfg=load_config(path if path.exists() else None)
    logs=Path(cfg["runtime"]["logs"]).expanduser()
    logs.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler()])
    if args.command == "close":
        if args.resume_session:
            parser.error("'close' cannot resume a session")
        if any((args.doctor, args.text, args.ensure_backend, args.scheduler_only,
                args.automations, args.automation_tick, args.reason,
                args.pending, args.approve is not None, args.reject is not None)):
            parser.error("'close' cannot be combined with another operation")
        stopped = []
        for role in ("voice", "scheduler"):
            if request_close(logs, role):
                stopped.append(role)
        print("Alfred stopped: " + ", ".join(stopped) if stopped else "Alfred is not running")
        return 0
    if args.resume_session and (
            args.doctor or args.ensure_backend or args.scheduler_only
            or args.automations or args.automation_tick or args.pending
            or args.approve is not None or args.reject is not None
            or args.workflow_draft or args.workflow_approve or args.workflow_list
            or args.workflow_start or args.workflow_runs
            or args.workflow_status is not None or args.workflow_cancel is not None
            or args.workflow_tick or args.workflow_reconcile is not None
            or args.workflow_schedule or args.workflow_triggers
            or args.workflow_trigger_pause is not None
            or args.workflow_trigger_resume is not None
            or args.workflow_trigger_cancel is not None):
        parser.error("--resume-session requires text, reason, or voice mode")
    if args.doctor:
        return 0 if doctor(cfg) else 2
    if args.ensure_backend:
        s=LlamaSupervisor(cfg["qwen"],logs)
        s.ensure_running()
        print(json.dumps(s.status()))
        return 0
    if (args.workflow_draft or args.workflow_approve or args.workflow_list
            or args.workflow_start or args.workflow_runs
            or args.workflow_status is not None or args.workflow_cancel is not None
            or args.workflow_tick or args.workflow_reconcile is not None
            or args.workflow_schedule or args.workflow_triggers
            or args.workflow_trigger_pause is not None
            or args.workflow_trigger_resume is not None
            or args.workflow_trigger_cancel is not None):
        app=Alfred(cfg,load_models=False)
        try:
            engine=app.tools.workflow_engine
            if args.workflow_draft:
                candidate=Path(args.workflow_draft).expanduser()
                if not candidate.is_file() or candidate.stat().st_size>40000:
                    raise ValueError("Workflow JSON must be a local file <=40 kB")
                draft=json.loads(candidate.read_text(encoding="utf-8"))
                result=engine.draft(draft)
            elif args.workflow_approve:
                result=engine.approve(args.workflow_approve)
            elif args.workflow_list:
                result=engine.definitions()
            elif args.workflow_start:
                result=engine.start(args.workflow_start)
            elif args.workflow_runs:
                result=engine.list_runs()
            elif args.workflow_status is not None:
                result=engine.details(args.workflow_status)
            elif args.workflow_cancel is not None:
                result=engine.cancel(args.workflow_cancel)
            elif args.workflow_reconcile is not None:
                result=engine.reconcile_interrupted(args.workflow_reconcile)
            elif args.workflow_schedule:
                parts=args.workflow_schedule.rsplit(":",1)
                if len(parts)!=2:
                    raise ValueError("Workflow schedule format must be NAME:MINUTES")
                result=engine.schedule_readonly(parts[0],int(parts[1]))
            elif args.workflow_triggers:
                result=engine.list_triggers()
            elif args.workflow_trigger_pause is not None:
                result=engine.change_trigger(args.workflow_trigger_pause,"pause")
            elif args.workflow_trigger_resume is not None:
                result=engine.change_trigger(args.workflow_trigger_resume,"resume")
            elif args.workflow_trigger_cancel is not None:
                result=engine.change_trigger(args.workflow_trigger_cancel,"cancel")
            else:
                result=engine.tick()
            print(json.dumps(result,indent=2,default=str))
        finally:
            app.close()
        return 0
    if args.automations or args.automation_tick or args.scheduler_only:
        app=Alfred(cfg,load_models=False)
        try:
            if args.automations:
                print(json.dumps(app.tools.automations.list_jobs(all_states=True),
                                 indent=2,default=str))
            elif args.automation_tick:
                app.tools.automations.tick(app.tools,app._remind)
            else:
                # Keep persistent timers and watches running independently of
                # the microphone and Qwen. Do not start llama watchdog.
                LOG.info("Starting headless Phase 3 scheduler")
                with ShutdownSocket(logs, "scheduler"):
                    app.scheduler.start()
                    try:
                        while True:
                            time.sleep(1)
                    except KeyboardInterrupt:
                        LOG.info("Stopping headless scheduler")
        finally:
            app.close()
        return 0
    # Administrative actions are intentionally CLI-only; they never appear in
    # Needle's tool schemas. No model needs loading to approve or reject.
    if args.pending or args.approve is not None or args.reject is not None:
        app=Alfred(cfg,load_models=False)
        try:
            if args.pending:
                print(json.dumps(app.system2.approvals.pending(),indent=2))
            elif args.approve is not None:
                print(json.dumps(app.system2.approve(args.approve),default=str,indent=2))
            else:
                print(json.dumps({"rejected":app.system2.approvals.reject(args.reject)}))
        finally:
            app.close()
        return 0
    if args.reason:
        app=Alfred(cfg,load_models=False)
        try:
            if args.resume_session:
                app.resume_session(args.resume_session)
            task = app._start_task("reason", len(args.reason))
            app.sessions.append(task.task_id, Decision("local", "delegate", 0))
            app.sessions.append(task.task_id,
                                Escalation("local", "qwen", "explicit_reason"))
            try:
                result = app.reason(args.reason)
                if result.get("status") == "incomplete":
                    app.sessions.append(task.task_id,
                                        Completion("failed", "invalid_plan"))
                elif "approval" in result:
                    app.sessions.append(task.task_id,
                                        ApprovalReference(result["approval"]["approval_id"]))
                    app.sessions.append(task.task_id,
                                        Completion("pending_approval", "needs_approval"))
                else:
                    app.sessions.append(task.task_id,
                                        Completion("completed" if cfg["qwen"]["enabled"] else "failed",
                                                   "no_action" if cfg["qwen"]["enabled"] else "disabled"))
                print(json.dumps(result,default=str,indent=2))
            except Exception:
                app.sessions.append(task.task_id, Completion("failed", "exception"))
                raise
        finally:
            app.close()
        # Unsuccessful planning must propagate a failing CLI exit code.
        return 1 if result.get("status") == "incomplete" else 0
    if not args.text:
        # Fail before loading native Whistle/Needle models or starting services.
        # Merely seeing a Pulse "Built-in Audio" entry is not enough.
        mic=AudioManager(cfg["audio"])
        try:
            selected=mic.ensure_input()
            LOG.info("Audio preflight passed: %s",selected)
        except AudioUnavailableError as exc:
            LOG.error("MICROPHONE NOT CONNECTED / NOT USABLE: %s",exc)
            return 2
        finally:
            mic.close()
    # Only the persistent voice instance advertises 'alfred close'.
    # One-shot text calls do not take over the voice control socket.
    control = ShutdownSocket(logs, "voice") if not args.text else nullcontext()
    with control:
        app=Alfred(cfg)
        try:
            if args.resume_session:
                app.resume_session(args.resume_session)
            app.start_services()
            if args.text:
                app.handle_text(args.text)
            else:
                app.listen()
        except KeyboardInterrupt:
            LOG.info("Stopping Alfred")
        except AudioUnavailableError as exc:
            LOG.error("Microphone disconnected or capture failed: %s",exc)
            return 2
        finally:
            app.close()
    return 0


def main(argv=None):
    """Systemd SIGTERM and terminal Ctrl+C both unwind Python finally blocks."""
    previous = signal.getsignal(signal.SIGTERM)
    def on_term(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, on_term)
    try:
        return _main(argv)
    except KeyboardInterrupt:
        LOG.info("Stopping Alfred")
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__=="__main__":
    raise SystemExit(main())
