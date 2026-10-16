"""Alfred voice orchestrator — Composition root.

Coordinates SessionManager, RequestRouter, and WakeWordListener into a unified assistant.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

# UtteranceStream is unused here but must stay importable as `alfred.app.UtteranceStream`:
# tests/test_wakeword.py and tests/test_voice_routing.py patch it on this module, and
# WakeWordListener.listen() looks it up via sys.modules["alfred.app"] to honour the patch.
from .audio import AudioManager, UtteranceStream  # noqa: F401
from .feedback import Feedback
from .models import Cactus, LlamaSupervisor, Qwen
from .inference_admission import AdmissionConfig, InferenceAdmission
from .lifecycle import ChildProcesses
from .runtime import TOOL_SCHEMAS, Scheduler, Store, Tools
from .system2 import System2
# AcousticWakeDetector is unused here but must stay importable as `alfred.app.AcousticWakeDetector`:
# tests/test_wakeword.py patches it on this module, and WakeWordListener.listen()
# resolves it via sys.modules["alfred.app"] so the patched class is the one instantiated.
from .wakeword import WakeWordGate, AcousticWakeDetector  # noqa: F401
from .skills import SkillRegistry
from .cognitive import SessionLedger
from .cognitive.harness_sdk import LocalHarness, HarnessUnavailable
# NeedleAgentLoop must stay importable as `alfred.app.NeedleAgentLoop`: tests/test_escalation.py
# patches "alfred.app.NeedleAgentLoop.run" to simulate a mismatched Needle result.
# (SessionSlots is used in annotations below.)
from .cognitive.needle_loop import SessionSlots, NeedleAgentLoop  # noqa: F401
from .cognitive.context import ContextManager
from .listener import WakeWordListener
from .router import RequestRouter
from .session import SessionManager

LOG = logging.getLogger(__name__)


class Alfred:
    """Composition root for Alfred's session, routing, listening, and model services."""

    def __init__(
        self,
        cfg: dict[str, Any],
        load_models: bool = True,
        *,
        meeting_adapter: Any = None,
    ) -> None:
        self.cfg = cfg
        self._build_routing_services()
        self._build_audio_services()
        self._build_model_services(load_models)
        self._build_lifecycle_services(meeting_adapter)

    def _build_routing_services(self) -> None:
        """Initialise Store, SessionManager, and InferenceAdmission."""
        runtime = self.cfg["runtime"]
        self.store = Store(runtime["database"])
        self.session_manager = SessionManager(self.store, runtime)
        self.admission = InferenceAdmission(
            AdmissionConfig(
                enabled=runtime["resource_admission_enabled"],
                min_available_mib=runtime["resource_min_available_mib"],
                max_swap_used_mib=runtime["resource_max_swap_used_mib"],
                max_cpu_load_per_core=runtime["resource_max_cpu_load_per_core"],
                max_wait_ms=runtime["resource_max_wait_ms"],
            )
        )

    def _build_audio_services(self) -> None:
        """Initialise AudioManager, WakeWordGate, and Feedback."""
        self.processes = ChildProcesses()
        self.feedback = Feedback(self.cfg["runtime"]["tts"], processes=self.processes)
        self.audio = AudioManager(self.cfg["audio"])
        self.wake_word = WakeWordGate(self.cfg["wake_word"])

    def _build_model_services(self, load_models: bool) -> None:
        """Initialise Tools, LlamaSupervisor, Qwen, System2, LocalHarness, and Cactus."""
        config = self.cfg
        runtime = config["runtime"]
        self.tools = Tools(runtime, self.store, processes=self.processes)
        self.supervisor = LlamaSupervisor(config["qwen"], runtime["logs"])
        self.qwen = (
            Qwen(config["qwen"], self.supervisor, admission=self.admission)
            if self.admission.enabled
            else Qwen(config["qwen"], self.supervisor)
        )
        self.system2 = System2(self.qwen, self.tools, self.store, runtime)

        model = config["qwen"]["model"] or config["qwen"]["expected_model"]
        self.harness = LocalHarness(
            self.supervisor.url,
            model,
            timeout=runtime["harness_timeout_seconds"],
            **({"admission": self.admission} if self.admission.enabled else {}),
        )
        if not runtime.get("telemetry", False):
            os.environ.setdefault("NEEDLE_TELEMETRY", "0")

        self.cactus = (
            Cactus(TOOL_SCHEMAS, admission=self.admission)
            if load_models and self.admission.enabled
            else Cactus(TOOL_SCHEMAS)
            if load_models
            else None
        )

    def _build_lifecycle_services(self, meeting_adapter: Any) -> None:
        """Initialise SkillRegistry, Scheduler, RequestRouter, WakeWordListener, and watchdog state."""
        self.skills = SkillRegistry(tools=self.tools, meeting=meeting_adapter)
        self.scheduler = Scheduler(self.store, self.tools, self._remind)
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._closed = False

        self.router = RequestRouter(
            cfg=self.cfg,
            store=self.store,
            session_manager=self.session_manager,
            system2=self.system2,
            supervisor=self.supervisor,
            harness=self.harness,
            admission=self.admission,
            feedback=self.feedback,
            tools=self.tools,
            cactus=self.cactus,
        )
        self.listener = WakeWordListener(
            cfg=self.cfg,
            audio=self.audio,
            wake_word=self.wake_word,
            session_manager=self.session_manager,
            router=self.router,
            feedback=self.feedback,
            admission=self.admission,
            cactus=self.cactus,
            handle_text_fn=self.handle_text,
        )

    # -----------------------------------------------------------------------
    # Delegated properties — backward-compat aliases onto sub-services
    # -----------------------------------------------------------------------

    # --- Session state aliases (callers use app.session_id / app.sessions / etc.) ---

    @property
    def session_id(self) -> str | None:
        # Backward-compat alias: external callers (cli, tests) access app.session_id.
        return self.session_manager.session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self.session_manager.session_id = value

    @property
    def sessions(self) -> SessionLedger:
        # Backward-compat alias: external callers (cli, tests) access app.sessions.
        return self.session_manager.sessions

    @sessions.setter
    def sessions(self, value: SessionLedger) -> None:
        self.session_manager.ledger = value
        self.session_manager.sessions = value

    @property
    def context(self) -> ContextManager:
        # Backward-compat alias: external callers (cli, tests) access app.context.
        return self.session_manager.context

    @context.setter
    def context(self, value: ContextManager) -> None:
        self.session_manager.context = value

    @property
    def needle_slots(self) -> SessionSlots:
        # Backward-compat alias: external callers (phase5_qa) access app.needle_slots.
        return self.session_manager.needle_slots

    @needle_slots.setter
    def needle_slots(self, value: SessionSlots) -> None:
        self.session_manager.needle_slots = value

    @property
    def _command_source(self) -> str:
        # Backward-compat alias: tests set app._command_source to simulate voice/text.
        return self.router._command_source

    @_command_source.setter
    def _command_source(self, value: str) -> None:
        self.router._command_source = value

    @property
    def _needle_cancel(self) -> threading.Event:
        # Backward-compat alias: tests call app._needle_cancel.set() to abort needle loops.
        return self.router._needle_cancel

    # --- Write-propagating service properties (assignment keeps sub-services in sync) ---

    @property
    def cactus(self) -> Cactus | None:
        # Write-propagating: assigning app.cactus also updates router and listener.
        return getattr(self, "_cactus", None)

    @cactus.setter
    def cactus(self, value: Cactus | None) -> None:
        self._cactus = value
        if hasattr(self, "router"):
            self.router.cactus = value
        if hasattr(self, "listener"):
            self.listener.cactus = value

    @property
    def harness(self) -> LocalHarness:
        # Write-propagating: assigning app.harness also updates router.
        return getattr(self, "_harness", None)

    @harness.setter
    def harness(self, value: LocalHarness) -> None:
        self._harness = value
        if hasattr(self, "router"):
            self.router.harness = value

    @property
    def system2(self) -> System2:
        # Write-propagating: assigning app.system2 also updates router.
        return getattr(self, "_system2", None)

    @system2.setter
    def system2(self, value: System2) -> None:
        self._system2 = value
        if hasattr(self, "router"):
            self.router.system2 = value

    @property
    def supervisor(self) -> LlamaSupervisor:
        # Write-propagating: assigning app.supervisor also updates router.
        return getattr(self, "_supervisor", None)

    @supervisor.setter
    def supervisor(self, value: LlamaSupervisor) -> None:
        self._supervisor = value
        if hasattr(self, "router"):
            self.router.supervisor = value

    @property
    def feedback(self) -> Feedback:
        # Write-propagating: assigning app.feedback also updates router and listener.
        return getattr(self, "_feedback", None)

    @feedback.setter
    def feedback(self, value: Feedback) -> None:
        self._feedback = value
        if hasattr(self, "router"):
            self.router.feedback = value
        if hasattr(self, "listener"):
            self.listener.feedback = value

    @property
    def tools(self) -> Tools:
        # Write-propagating: assigning app.tools also updates router.
        return getattr(self, "_tools", None)

    @tools.setter
    def tools(self, value: Tools) -> None:
        self._tools = value
        if hasattr(self, "router"):
            self.router.tools = value

    @property
    def store(self) -> Store:
        # Write-propagating: assigning app.store also updates router and session_manager.
        return getattr(self, "_store", None)

    @store.setter
    def store(self, value: Store) -> None:
        self._store = value
        if hasattr(self, "router"):
            self.router.store = value
        if hasattr(self, "session_manager"):
            self.session_manager.store = value

    @property
    def admission(self) -> InferenceAdmission:
        # Write-propagating: assigning app.admission also updates router and listener.
        return getattr(self, "_admission", None)

    @admission.setter
    def admission(self, value: InferenceAdmission) -> None:
        self._admission = value
        if hasattr(self, "router"):
            self.router.admission = value
        if hasattr(self, "listener"):
            self.listener.admission = value

    # --- Delegated methods exposed as properties for mockability ---

    @property
    def reason(self) -> Any:
        # Backward-compat alias: cli and tests call/replace app.reason.
        return self.router.reason

    @reason.setter
    def reason(self, value: Any) -> None:
        self.router.reason = value

    @property
    def handle_text(self) -> Any:
        # Backward-compat alias: cli and tests call/replace app.handle_text.
        return self.router.handle_text

    @handle_text.setter
    def handle_text(self, value: Any) -> None:
        self.router.handle_text = value
        if hasattr(self, "listener"):
            self.listener.handle_text_fn = value

    @property
    def _start_task(self) -> Any:
        # Backward-compat alias: cli and tests call app._start_task to open a task envelope.
        return self.session_manager.start_task

    @_start_task.setter
    def _start_task(self, value: Any) -> None:
        self.session_manager.start_task = value

    @property
    def resume_session(self) -> Any:
        # Backward-compat alias: cli and tests call app.resume_session(sid).
        return self._resume_session_wrapper

    @resume_session.setter
    def resume_session(self, value: Any) -> None:
        self.session_manager.resume_session = value

    def _resume_session_wrapper(self, session_id: str) -> str:
        with self.router._needle_execution_lock:
            return self.session_manager.resume_session(session_id)

    # -----------------------------------------------------------------------
    # Service lifecycle
    # -----------------------------------------------------------------------

    def _remind(self, event: dict[str, Any]) -> None:
        text = f"Reminder #{event['id']}: {event['message']}"
        self.feedback.tell(text)
        self.store.event("reminder", event)

    def start_services(self) -> None:
        self.scheduler.start()
        if self.cfg["qwen"]["enabled"]:

            def watchdog() -> None:
                while not self._watchdog_stop.is_set():
                    try:
                        if self.admission.enabled:
                            if not self.admission.capture_active:
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
                target=watchdog, name="model-supervisor", daemon=True
            )
            self._watchdog_thread.start()

    def require_cognitive_runtime(self) -> dict[str, Any]:
        """Audit installed Cordis graph and launch SDK before an interactive task."""
        if not self.cfg["qwen"]["enabled"]:
            raise HarnessUnavailable("Required Harness needs Qwen enabled")
        diagnosis = self.harness.audit_installation()
        with self.admission.claim("qwen"):
            self.supervisor.ensure_running()
            served_model = self.supervisor.model_id()
        self.harness.configure_model(served_model)
        self.harness.require_ready()
        LOG.info(
            "Official Cordis SDK active (tool-free foundation, shared tools PENDING): "
            "sdk=%s runtime=%s profile=%s",
            diagnosis["sdk_version"],
            diagnosis["runtime_version"],
            diagnosis["profile"],
        )
        return diagnosis

    def listen(self) -> None:
        return self.listener.listen()

    def close(self) -> None:
        """Idempotent teardown; never terminate an inference server we adopted."""
        if self._closed:
            return
        self._closed = True
        self.admission.close()
        self._watchdog_stop.set()
        self.router._needle_cancel.set()
        self.session_manager.close()
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
        threads = (
            self.scheduler.thread,
            self.scheduler.workflow_thread,
            self._watchdog_thread,
        )
        if any(t is not None and t.is_alive() for t in threads):
            LOG.warning(
                "Background workers did not all exit; leaving DB open until process exit"
            )
        else:
            if self.router._needle_execution_lock.acquire(blocking=False):
                try:
                    self.session_manager.release_ownership()
                finally:
                    self.router._needle_execution_lock.release()
            else:
                LOG.warning("Active command still owns its session; retaining ownership")
            self.store.db.close()


# ---------------------------------------------------------------------------
# Backward-compatibility re-exports: doctor, _main, main moved to alfred.cli
# ---------------------------------------------------------------------------
from .cli import doctor, _main, main  # noqa: E402,F401  (must follow Alfred class)
