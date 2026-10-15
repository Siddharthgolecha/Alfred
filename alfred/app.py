"""Alfred voice orchestrator — Composition root.

Coordinates SessionManager, RequestRouter, and WakeWordListener into a unified assistant.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from .audio import AudioManager, UtteranceStream  # noqa: F401 (patch target)
from .feedback import Feedback
from .models import Cactus, LlamaSupervisor, Qwen
from .inference_admission import AdmissionConfig, InferenceAdmission
from .lifecycle import ChildProcesses
from .runtime import TOOL_SCHEMAS, Scheduler, Store, Tools
from .system2 import System2
from .wakeword import WakeWordGate, AcousticWakeDetector  # noqa: F401 (patch target)
from .skills import SkillRegistry
from .cognitive import SessionLedger
from .cognitive.harness_sdk import LocalHarness, HarnessUnavailable
from .cognitive.needle_loop import SessionSlots, NeedleAgentLoop  # noqa: F401 (patch target)
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
        runtime = cfg["runtime"]
        self.store = Store(runtime["database"])
        self.session_manager = SessionManager(self.store, runtime)
        self.processes = ChildProcesses()
        self.admission = InferenceAdmission(
            AdmissionConfig(
                enabled=runtime["resource_admission_enabled"],
                min_available_mib=runtime["resource_min_available_mib"],
                max_swap_used_mib=runtime["resource_max_swap_used_mib"],
                max_cpu_load_per_core=runtime["resource_max_cpu_load_per_core"],
                max_wait_ms=runtime["resource_max_wait_ms"],
            )
        )
        self.tools = Tools(runtime, self.store, processes=self.processes)
        self.skills = SkillRegistry(tools=self.tools, meeting=meeting_adapter)
        self.feedback = Feedback(runtime["tts"], processes=self.processes)
        self.audio = AudioManager(cfg["audio"])
        self.wake_word = WakeWordGate(cfg["wake_word"])
        self.supervisor = LlamaSupervisor(cfg["qwen"], runtime["logs"])
        self.qwen = (
            Qwen(cfg["qwen"], self.supervisor, admission=self.admission)
            if self.admission.enabled
            else Qwen(cfg["qwen"], self.supervisor)
        )
        self.system2 = System2(self.qwen, self.tools, self.store, runtime)

        model = cfg["qwen"]["model"] or cfg["qwen"]["expected_model"]
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
        self.scheduler = Scheduler(self.store, self.tools, self._remind)
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._closed = False

        self.router = RequestRouter(
            cfg=cfg,
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
            cfg=cfg,
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
    # Delegated properties for backwards compatibility
    # -----------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self.session_manager.session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self.session_manager.session_id = value

    @property
    def _active_task_id(self) -> str | None:
        return self.session_manager.active_task_id

    @_active_task_id.setter
    def _active_task_id(self, value: str | None) -> None:
        self.session_manager.active_task_id = value

    @property
    def sessions(self) -> SessionLedger:
        return self.session_manager.sessions

    @sessions.setter
    def sessions(self, value: SessionLedger) -> None:
        self.session_manager.ledger = value
        self.session_manager.sessions = value

    @property
    def context(self) -> ContextManager:
        return self.session_manager.context

    @context.setter
    def context(self, value: ContextManager) -> None:
        self.session_manager.context = value

    @property
    def needle_slots(self) -> SessionSlots:
        return self.session_manager.needle_slots

    @needle_slots.setter
    def needle_slots(self, value: SessionSlots) -> None:
        self.session_manager.needle_slots = value

    @property
    def _command_source(self) -> str:
        return self.router._command_source

    @_command_source.setter
    def _command_source(self, value: str) -> None:
        self.router._command_source = value

    @property
    def _needle_execution_lock(self) -> threading.RLock:
        return self.router._needle_execution_lock

    @property
    def _needle_task_lock(self) -> threading.RLock:
        return self.router._needle_execution_lock

    @property
    def _needle_cancel(self) -> threading.Event:
        return self.router._needle_cancel

    @property
    def cactus(self) -> Cactus | None:
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
        return getattr(self, "_harness", None)

    @harness.setter
    def harness(self, value: LocalHarness) -> None:
        self._harness = value
        if hasattr(self, "router"):
            self.router.harness = value

    @property
    def system2(self) -> System2:
        return getattr(self, "_system2", None)

    @system2.setter
    def system2(self, value: System2) -> None:
        self._system2 = value
        if hasattr(self, "router"):
            self.router.system2 = value

    @property
    def supervisor(self) -> LlamaSupervisor:
        return getattr(self, "_supervisor", None)

    @supervisor.setter
    def supervisor(self, value: LlamaSupervisor) -> None:
        self._supervisor = value
        if hasattr(self, "router"):
            self.router.supervisor = value

    @property
    def feedback(self) -> Feedback:
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
        return getattr(self, "_tools", None)

    @tools.setter
    def tools(self, value: Tools) -> None:
        self._tools = value
        if hasattr(self, "router"):
            self.router.tools = value

    @property
    def store(self) -> Store:
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
        return getattr(self, "_admission", None)

    @admission.setter
    def admission(self, value: InferenceAdmission) -> None:
        self._admission = value
        if hasattr(self, "router"):
            self.router.admission = value
        if hasattr(self, "listener"):
            self.listener.admission = value

    # -----------------------------------------------------------------------
    # Delegated methods as properties / setters for mockability
    # -----------------------------------------------------------------------

    @property
    def reason(self) -> Any:
        return self.router.reason

    @reason.setter
    def reason(self, value: Any) -> None:
        self.router.reason = value

    @property
    def handle_text(self) -> Any:
        return self.router.handle_text

    @handle_text.setter
    def handle_text(self, value: Any) -> None:
        self.router.handle_text = value
        if hasattr(self, "listener"):
            self.listener.handle_text_fn = value

    @property
    def _handle_text_locked(self) -> Any:
        return self.router._handle_text_locked

    @_handle_text_locked.setter
    def _handle_text_locked(self, value: Any) -> None:
        self.router._handle_text_locked = value

    @property
    def _process_authorized_task(self) -> Any:
        return self.router._process_authorized_task

    @_process_authorized_task.setter
    def _process_authorized_task(self, value: Any) -> None:
        self.router._process_authorized_task = value

    @property
    def _handoff(self) -> Any:
        return self.router._handoff

    @_handoff.setter
    def _handoff(self, value: Any) -> None:
        self.router._handoff = value

    @property
    def _observe_needle_prefix(self) -> Any:
        return self.router._observe_needle_prefix

    @_observe_needle_prefix.setter
    def _observe_needle_prefix(self, value: Any) -> None:
        self.router._observe_needle_prefix = value

    @property
    def _finish_handoff(self) -> Any:
        return self.router._finish_handoff

    @_finish_handoff.setter
    def _finish_handoff(self, value: Any) -> None:
        self.router._finish_handoff = value

    @property
    def _start_task(self) -> Any:
        return self.session_manager.start_task

    @_start_task.setter
    def _start_task(self, value: Any) -> None:
        self.session_manager.start_task = value

    @property
    def resume_session(self) -> Any:
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
