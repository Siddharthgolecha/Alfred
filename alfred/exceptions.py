"""Alfred domain exceptions and error codes."""
from __future__ import annotations

from enum import Enum


class ErrorCode(Enum):
    """Standardized error codes for Alfred domain failures."""

    # Tool execution
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_VALIDATION_FAILED = "tool_validation_failed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"

    # Session and task lifecycle
    SESSION_NOT_AUTHORIZED = "session_not_authorized"
    SESSION_RECORD_FAILED = "session_record_failed"
    TASK_FAILED = "task_failed"

    # Inference backends
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_STARTUP_TIMEOUT = "backend_startup_timeout"
    BACKEND_MODEL_MISMATCH = "backend_model_mismatch"

    # Routing and planning
    ROUTING_FAILED = "routing_failed"
    PLAN_VALIDATION_FAILED = "plan_validation_failed"
    HANDOFF_FAILED = "handoff_failed"

    # Audio / STT
    AUDIO_UNAVAILABLE = "audio_unavailable"
    STT_ENGINE_FAILED = "stt_engine_failed"

    # Scheduler
    SCHEDULER_EVENT_FAILED = "scheduler_event_failed"
    AUTOMATION_TICK_FAILED = "automation_tick_failed"
    WORKFLOW_TICK_FAILED = "workflow_tick_failed"

    # Resource admission
    ADMISSION_REJECTED = "admission_rejected"


class AlfredError(Exception):
    """Base class for all Alfred domain exceptions."""

    def __init__(self, message: str, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        if self.code is not None:
            return f"{type(self).__name__}({self.code.value!r}: {self.message!r})"
        return f"{type(self).__name__}({self.message!r})"


class ToolError(AlfredError):
    """Raised when a tool cannot be found, validated, or executed."""


class SessionError(AlfredError):
    """Raised for session lifecycle failures (authorization, recording)."""


class RoutingError(AlfredError):
    """Raised when the request router encounters an unrecoverable failure."""


class PlanError(AlfredError):
    """Raised when a Qwen plan fails validation or cannot be staged."""


class BackendError(AlfredError):
    """Raised for inference backend startup, health, or model-selection failures."""


class SchedulerError(AlfredError):
    """Raised when a scheduled event, automation tick, or workflow step fails."""
