"""Alfred CLI entry-points: doctor diagnostics, argument parsing, and main()."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import logging
import signal
import time
from pathlib import Path

from .audio import AudioManager, AudioUnavailableError, pactl_json
from .config import load_config
from .models import LlamaSupervisor
from .inference_admission import AdmissionConfig, AdmissionRejected, InferenceAdmission
from .lifecycle import ShutdownSocket, request_close
from .cognitive.harness_sdk import LocalHarness, HarnessUnavailable
from .cognitive import Decision, Escalation, ApprovalReference, Completion
from .app import Alfred
from .platform_utils import IS_LINUX
from .ui import TerminalUI
from .output_capture import NativeOutputCapture

LOG = logging.getLogger(__name__)


class _UiLogHandler(logging.Handler):
    def __init__(self, ui):
        super().__init__(logging.DEBUG)
        self.ui = ui

    def emit(self, record):
        self.ui.log(record)


def _run_conversation(args, config, logs):
    """Run voice/text/reason with isolated presentation and output capture."""
    ui = TerminalUI(debug=args.debug)
    root = logging.getLogger()
    previous_level = root.level
    previous_handlers = list(root.handlers)
    for handler in previous_handlers:
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG if args.debug else logging.CRITICAL)
    root.addHandler(_UiLogHandler(ui) if args.debug else logging.NullHandler())
    app = None
    exit_code = 0
    try:
        ui.start()
        with NativeOutputCapture(debug=args.debug):
            try:
                if args.reason:
                    app = Alfred(config, load_models=False, ui=ui)
                    ui.activity("Connecting to the local reasoning service…")
                    app.require_cognitive_runtime()
                    if args.resume_session:
                        app.resume_session(args.resume_session)
                    task = app._start_task("reason", len(args.reason))
                    ui.begin_request("reason")
                    ui.user(args.reason)
                    app.sessions.append(task.task_id,
                                        Decision(engine="local", route="delegate", call_count=0))
                    app.sessions.append(task.task_id,
                                        Escalation(source_model="local", target_model="qwen",
                                                   reason="explicit_reason"))
                    try:
                        result = app.reason(args.reason)
                        if result.get("status") == "incomplete":
                            app.sessions.append(task.task_id,
                                                Completion(outcome="failed", reason="invalid_plan"))
                        elif "approval" in result:
                            app.sessions.append(
                                task.task_id,
                                ApprovalReference(approval_id=result["approval"]["approval_id"]))
                            app.sessions.append(task.task_id,
                                                Completion(outcome="pending_approval",
                                                           reason="needs_approval"))
                        else:
                            app.sessions.append(task.task_id, Completion(
                                outcome="completed" if config["qwen"]["enabled"] else "failed",
                                reason="no_action" if config["qwen"]["enabled"] else "disabled"))
                        ui.present_reply(json.dumps(result, default=str))
                        exit_code = 1 if result.get("status") == "incomplete" else 0
                    except Exception:
                        app.sessions.append(task.task_id,
                                            Completion(outcome="failed", reason="exception"))
                        ui.request_state = "failed"
                        raise
                    finally:
                        ui.finish_request(ui.request_state)
                else:
                    if not args.text:
                        mic = AudioManager(config["audio"])
                        try:
                            mic.ensure_input()
                        except AudioUnavailableError:
                            ui.reply("Microphone unavailable. Check the audio input and try again.")
                            return 2
                        finally:
                            mic.close()
                    control = ShutdownSocket(logs, "voice") if not args.text else nullcontext()
                    with control:
                        app = Alfred(config, ui=ui)
                        ui.activity("Loading local models…")
                        app.require_cognitive_runtime()
                        if args.resume_session:
                            app.resume_session(args.resume_session)
                        app.start_services()
                        if args.text:
                            app.handle_text(args.text)
                        else:
                            ui.activity("Listening for wake word…")
                            app.listen()
            except KeyboardInterrupt:
                LOG.info("Stopping Alfred")
                if ui.trace_active:
                    ui.finish_request("interrupted")
            except AudioUnavailableError:
                ui.request_state = "failed"
                ui.reply("Microphone disconnected. Check the audio input and restart Alfred.")
                exit_code = 2
            except Exception as exc:
                LOG.exception("Alfred request failed")
                ui.request_state = "failed"
                detail = "Run with --debug for details." if not args.debug else (
                    f"{type(exc).__name__}. See the debug trace for details.")
                ui.reply(f"Alfred couldn't complete startup or the request. {detail}")
                exit_code = 2
            finally:
                if app is not None:
                    try:
                        app.close()
                    except Exception:
                        LOG.exception("Alfred shutdown failed")
                        if exit_code == 0:
                            exit_code = 2
        if args.debug and (args.text or args.reason) and ui.interactive:
            ui.browser()
        return exit_code
    finally:
        ui.close()
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)


def _diagnose_audio(cfg) -> bool:
    print("Audio input mode:", cfg["audio"]["mode"])
    manager = AudioManager(cfg["audio"])
    healthy = False
    try:
        entries = manager.diagnostics()
        if not entries:
            print("Audio server lists no recording sources.")
        for source in entries:
            tag = "CANDIDATE" if source["eligible"] else "REJECTED"
            print(f"[{tag}] {source['description'] or source['name']}: "
                  f"{source['name']} — {source['reason']}")
        try:
            selected = manager.ensure_input()
            print(f"MICROPHONE INPUT STREAM VERIFIED: {selected}")
            healthy = True
        except AudioUnavailableError as exc:
            print(f"MICROPHONE NOT CONNECTED / NOT USABLE: {exc}")
    except (AudioUnavailableError, OSError, RuntimeError) as exc:
        print(f"MICROPHONE NOT CONNECTED / AUDIO SERVER FAILURE: {exc}")
    finally:
        manager.close()
    if IS_LINUX:
        try:
            for card in pactl_json("cards"):
                if str(card.get("name", "")).startswith("bluez_card"):
                    print("Bluetooth card:", card.get("name"),
                          "profile:", card.get("active_profile"))
        except (OSError, ValueError, RuntimeError) as exc:
            print("Bluetooth profile detection unavailable:", exc)
    return healthy


def _diagnose_qwen(cfg) -> tuple[LlamaSupervisor, dict]:
    supervisor = LlamaSupervisor(cfg["qwen"], cfg["runtime"]["logs"])
    backend = supervisor.status()
    print("Inference backend:", json.dumps(backend))
    print("Model:", cfg["qwen"]["model"] or cfg["qwen"]["model_path"] or "(auto-select)")
    print("Backend executable:", supervisor.executable_name())
    return supervisor, backend


def _diagnose_harness(cfg, supervisor: LlamaSupervisor, backend: dict) -> bool:
    harness_ready = False
    harness = None
    runtime = cfg["runtime"]
    if not cfg["qwen"]["enabled"]:
        print("Harness NOT READY: inference backend is disabled")
        return False
    if backend.get("health") != "ready":
        print("Harness NOT READY: inference backend is not responding at " + supervisor.url)
        return False
    if not backend.get("model"):
        try:
            supervisor.model_id()
        except (RuntimeError, OSError, ValueError) as exc:
            print("Model readiness:", str(exc))
        print("Harness NOT READY: configured Qwen model is not served by the backend")
        return False
    # --doctor launches a real Cordis child when the model is READY.
    # Apply the very same memory/swap/CPU/capture admission as interactive
    # startup; diagnostics must never bypass the deployment pressure gate.
    admission = InferenceAdmission(
        AdmissionConfig(
            enabled=runtime["resource_admission_enabled"],
            min_available_mib=runtime["resource_min_available_mib"],
            max_swap_used_mib=runtime["resource_max_swap_used_mib"],
            max_cpu_load_per_core=runtime["resource_max_cpu_load_per_core"],
            max_wait_ms=runtime["resource_max_wait_ms"],
        )
    )
    try:
        harness = LocalHarness(
            supervisor.url, cfg["qwen"]["model"] or cfg["qwen"]["expected_model"],
            timeout=runtime["harness_timeout_seconds"],
            admission=admission)
        status = harness.audit_installation()
        print("Official DeepSeek Harness plugin audit:", json.dumps(status))
        print("Cordis tool bridge: NOT YET WIRED (Issue #58; zero model-facing tools)")
        harness.configure_model(backend["model"])
        harness.require_ready()  # Actual SDK child, not merely a YAML audit.
        harness_ready = True
        print("Official Cordis SDK subprocess: READY (restricted/tool-free)")
    except (HarnessUnavailable, AdmissionRejected, OSError, ValueError) as exc:
        print("Harness NOT READY:", str(exc))
    finally:
        if harness is not None:
            harness.close()
        admission.close()
    return harness_ready


def doctor(cfg) -> bool:
    """Make advertised devices and *verified live microphone* visibly distinct."""
    audio_healthy = _diagnose_audio(cfg)
    supervisor, backend = _diagnose_qwen(cfg)
    harness_ready = _diagnose_harness(cfg, supervisor, backend)
    return audio_healthy and harness_ready


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Alfred Jetson voice automation agent")
    parser.add_argument("--config", default="config.json", help="Local JSON config")
    parser.add_argument("--debug", action="store_true",
                        help="Show diagnostic logs, live system stats, and request traces")
    parser.add_argument("--resume-session", metavar="SESSION_ID",
                        help="Explicitly reuse a prior local session ID; never replay interrupted tools")
    parser.add_argument("command", nargs="?", choices=["close"],
                        help="Close running Alfred services and processes owned by Alfred")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--doctor", action="store_true",
                       help="Show mic/server status without models")
    group.add_argument("--text", help="Execute one command using Needle and tools")
    group.add_argument("--ensure-backend", "--ensure-llama", dest="ensure_backend",
                       action="store_true",
                       help="Start/verify the selected inference backend then exit")
    group.add_argument("--scheduler-only", action="store_true",
                       help="Run reminders and watches without microphone, Cactus, or Qwen")
    group.add_argument("--automations", action="store_true",
                       help="List Phase 3 automations without loading native models")
    group.add_argument("--automation-tick", action="store_true",
                       help="Run one scheduler cycle for debugging and exit")
    group.add_argument("--reason",
                       help="Test Qwen planner on a text input; no Cactus engine required")
    group.add_argument("--skills", action="store_true",
                       help="List available and unavailable capabilities without models")
    group.add_argument("--memory-set", metavar="KEY=VALUE",
                       help="Explicitly retain a short preference from the local CLI")
    group.add_argument("--memory-list", action="store_true",
                       help="List approved retained facts for this context scope")
    group.add_argument("--memory-clear", metavar="KEY|ALL",
                       help="Delete one/all retained facts in this scope")
    group.add_argument("--pending", action="store_true",
                       help="List proposed tool actions awaiting approval")
    group.add_argument("--approve", type=int, metavar="ID",
                       help="Locally approve and execute a proposal once")
    group.add_argument("--reject", type=int, metavar="ID",
                       help="Locally reject a proposal")
    group.add_argument("--minutes", action="store_true",
                       help="Directly queue the approved 'meeting_minutes' workflow and exit")
    group.add_argument("--workflow-draft", metavar="JSON_FILE",
                       help="Validate and save a local workflow graph as unapproved draft")
    group.add_argument("--workflow-approve", metavar="NAME",
                       help="Locally approve a validated graph version")
    group.add_argument("--workflow-list", action="store_true",
                       help="List graph definitions and their approval state")
    group.add_argument("--workflow-start", metavar="NAME",
                       help="Locally queue an approved graph workflow")
    group.add_argument("--workflow-runs", action="store_true",
                       help="List recent workflow execution statuses")
    group.add_argument("--workflow-status", type=int, metavar="RUN_ID",
                       help="Inspect durable step results for a workflow run")
    group.add_argument("--workflow-cancel", type=int, metavar="RUN_ID",
                       help="Cancel pending steps of an in-progress graph")
    group.add_argument("--workflow-tick", action="store_true",
                       help="Execute up to three ready graph workflow steps")
    group.add_argument("--workflow-reconcile", type=int, metavar="RUN_ID",
                       help="Explicitly mark an uncertain in-flight step interrupted")
    group.add_argument("--workflow-schedule", metavar="NAME:MINUTES",
                       help="Schedule a locally approved read-only graph on an interval")
    group.add_argument("--workflow-triggers", action="store_true",
                       help="List recurring read-only workflow triggers")
    group.add_argument("--workflow-trigger-pause", type=int, metavar="TRIGGER_ID")
    group.add_argument("--workflow-trigger-resume", type=int, metavar="TRIGGER_ID")
    group.add_argument("--workflow-trigger-cancel", type=int, metavar="TRIGGER_ID")

    args = parser.parse_args(argv)
    path = Path(args.config)
    config = load_config(path if path.exists() else None)
    logs = Path(config["runtime"]["logs"]).expanduser()
    logs.mkdir(parents=True, exist_ok=True)
    if args.debug and not (args.text or args.reason or not any((
            args.doctor, args.ensure_backend, args.scheduler_only, args.automations,
            args.automation_tick, args.skills, args.memory_set is not None,
            args.memory_list, args.memory_clear is not None, args.pending,
            args.approve is not None, args.reject is not None, args.minutes,
            args.workflow_draft, args.workflow_approve, args.workflow_list,
            args.workflow_start, args.workflow_runs, args.workflow_status is not None,
            args.workflow_cancel is not None, args.workflow_tick,
            args.workflow_reconcile is not None, args.workflow_schedule,
            args.workflow_triggers, args.workflow_trigger_pause is not None,
            args.workflow_trigger_resume is not None,
            args.workflow_trigger_cancel is not None, args.command))):
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "close":
        if args.resume_session:
            parser.error("'close' cannot resume a session")
        if any((args.doctor, args.text, args.ensure_backend, args.scheduler_only,
                args.automations, args.automation_tick, args.reason, args.skills,
                args.pending, args.approve is not None, args.reject is not None,
                args.memory_set is not None, args.memory_list,
                args.memory_clear is not None)):
            parser.error("'close' cannot be combined with another operation")
        stopped = []
        for role in ("voice", "scheduler"):
            if request_close(logs, role):
                stopped.append(role)
        print("Alfred stopped: " + ", ".join(stopped) if stopped
              else "Alfred is not running")
        return 0
    if args.resume_session and (
            args.skills or args.doctor or args.ensure_backend or args.scheduler_only
            or args.automations or args.automation_tick or args.pending
            or args.approve is not None or args.reject is not None
            or args.minutes
            or args.workflow_draft or args.workflow_approve or args.workflow_list
            or args.workflow_start or args.workflow_runs
            or args.workflow_status is not None or args.workflow_cancel is not None
            or args.workflow_tick or args.workflow_reconcile is not None
            or args.workflow_schedule or args.workflow_triggers
            or args.workflow_trigger_pause is not None
            or args.workflow_trigger_resume is not None
            or args.workflow_trigger_cancel is not None):
        parser.error("--resume-session requires text, reason, or voice mode")
    if args.skills:
        app = Alfred(config, load_models=False)
        try:
            print(json.dumps(app.skills.metadata(), indent=2))
        finally:
            app.close()
        return 0
    if args.memory_set is not None or args.memory_list or args.memory_clear is not None:
        if not (config["runtime"]["context_enabled"]
                and config["runtime"]["context_persistence_enabled"]):
            parser.error("context and context persistence must both be opted into")
        app = Alfred(config, load_models=False)
        try:
            if args.memory_set is not None:
                if "=" not in args.memory_set:
                    parser.error("--memory-set requires KEY=VALUE")
                key, value = args.memory_set.split("=", 1)
                app.context.set_explicit_fact(key, value, local_approval=True)
                print(json.dumps({"retained": key,
                                  "scope": config["runtime"]["context_mode"]}))
            elif args.memory_list:
                print(json.dumps(app.context.list_facts(), indent=2))
            else:
                app.context.clear_facts(
                    None if args.memory_clear == "ALL" else args.memory_clear,
                    local_approval=True)
                print(json.dumps({"cleared": args.memory_clear}))
        finally:
            app.close()
        return 0
    if args.doctor:
        return 0 if doctor(config) else 2
    if args.ensure_backend:
        supervisor = LlamaSupervisor(config["qwen"], logs)
        supervisor.ensure_running()
        print(json.dumps(supervisor.status()))
        return 0
    if args.minutes:
        app = Alfred(config, load_models=False)
        try:
            result = app.tools.workflow_engine.start("meeting_minutes")
            print(json.dumps(result, indent=2, default=str))
        finally:
            app.close()
        return 0
    if (args.workflow_draft or args.workflow_approve or args.workflow_list
            or args.workflow_start or args.workflow_runs
            or args.workflow_status is not None or args.workflow_cancel is not None
            or args.workflow_tick or args.workflow_reconcile is not None
            or args.workflow_schedule or args.workflow_triggers
            or args.workflow_trigger_pause is not None
            or args.workflow_trigger_resume is not None
            or args.workflow_trigger_cancel is not None):
        app = Alfred(config, load_models=False)
        try:
            engine = app.tools.workflow_engine
            if args.workflow_draft:
                candidate = Path(args.workflow_draft).expanduser()
                if not candidate.is_file() or candidate.stat().st_size > 40000:
                    raise ValueError("Workflow JSON must be a local file <=40 kB")
                draft = json.loads(candidate.read_text(encoding="utf-8"))
                result = engine.draft(draft)
            elif args.workflow_approve:
                result = engine.approve(args.workflow_approve)
            elif args.workflow_list:
                result = engine.definitions()
            elif args.workflow_start:
                result = engine.start(args.workflow_start)
            elif args.workflow_runs:
                result = engine.list_runs()
            elif args.workflow_status is not None:
                result = engine.details(args.workflow_status)
            elif args.workflow_cancel is not None:
                result = engine.cancel(args.workflow_cancel)
            elif args.workflow_reconcile is not None:
                result = engine.reconcile_interrupted(args.workflow_reconcile)
            elif args.workflow_schedule:
                parts = args.workflow_schedule.rsplit(":", 1)
                if len(parts) != 2:
                    raise ValueError("Workflow schedule format must be NAME:MINUTES")
                result = engine.schedule_readonly(parts[0], int(parts[1]))
            elif args.workflow_triggers:
                result = engine.list_triggers()
            elif args.workflow_trigger_pause is not None:
                result = engine.change_trigger(args.workflow_trigger_pause, "pause")
            elif args.workflow_trigger_resume is not None:
                result = engine.change_trigger(args.workflow_trigger_resume, "resume")
            elif args.workflow_trigger_cancel is not None:
                result = engine.change_trigger(args.workflow_trigger_cancel, "cancel")
            else:
                result = engine.tick()
            print(json.dumps(result, indent=2, default=str))
        finally:
            app.close()
        return 0
    if args.automations or args.automation_tick or args.scheduler_only:
        app = Alfred(config, load_models=False)
        try:
            if args.automations:
                print(json.dumps(app.tools.automations.list_jobs(all_states=True),
                                 indent=2, default=str))
            elif args.automation_tick:
                app.tools.automations.tick(app.tools, app._remind)
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
        app = Alfred(config, load_models=False)
        try:
            if args.pending:
                print(json.dumps(app.system2.approvals.pending(), indent=2))
            elif args.approve is not None:
                print(json.dumps(app.system2.approve(args.approve),
                                 default=str, indent=2))
            else:
                print(json.dumps({"rejected":
                                  app.system2.approvals.reject(args.reject)}))
        finally:
            app.close()
        return 0
    if args.reason:
        return _run_conversation(args, config, logs)
    return _run_conversation(args, config, logs)


def main(argv=None) -> int:
    """Systemd/macOS SIGTERM and terminal Ctrl+C both unwind Python finally blocks."""
    # SIGTERM is a real signal on POSIX (Linux, macOS); on Windows it is supported
    # by Python's signal module but behaves identically to KeyboardInterrupt.
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
