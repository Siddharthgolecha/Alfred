"""Session lifecycle and context management for Alfred."""
from __future__ import annotations

import logging
from typing import Any

from .cognitive import SessionLedger, TaskEnvelope
from .cognitive.context import ContextManager
from .cognitive.needle_loop import SessionSlots
from .runtime import Store

LOG = logging.getLogger(__name__)


class SessionManager:
    """Manages session lifecycle, task envelopes, context state, and slot memory."""

    def __init__(
        self,
        store: Store,
        runtime_config: dict[str, Any],
    ) -> None:
        self.store = store
        self.ledger = SessionLedger(self.store)
        self.sessions = self.ledger  # backward-compatibility alias
        self.session_id: str | None = None
        self.active_task_id: str | None = None
        self.needle_slots = SessionSlots(runtime_config["needle_context_ttl_seconds"])
        self.context = ContextManager(
            self.store,
            enabled=runtime_config["context_enabled"],
            persist_facts=runtime_config["context_persistence_enabled"],
            owner=runtime_config["context_owner"],
            mode=runtime_config["context_mode"],
            ttl=runtime_config["context_ttl_seconds"],
            budget_chars=runtime_config["context_budget_chars"],
        )

    def start_task(self, source: str, input_chars: int) -> TaskEnvelope:
        """Begin a durable task envelope, reusing or generating session_id."""
        envelope = self.ledger.begin_task(source, input_chars, self.session_id)
        self.session_id = envelope.session_id
        self.active_task_id = envelope.task_id
        return envelope

    def resume_session(self, session_id: str) -> str:
        """Explicit local restart recovery; never restart an uncertain action."""
        if self.session_id is not None:
            raise ValueError("Cannot change sessions while active")
        self.ledger.resume_session(session_id)
        self.session_id = session_id
        self.needle_slots.clear()
        return session_id

    def clear_active_task(self) -> None:
        """Reset the active task identifier when a task finishes."""
        self.active_task_id = None

    def clear_session(
        self,
        session_id: str | None = None,
        *,
        task_id: str | None = None,
    ) -> None:
        """Clear context and slots for a given session or the current session."""
        target_session_id = session_id or self.session_id
        if target_session_id is not None:
            if self.context.enabled:
                self.context.clear_session(target_session_id, task_id=task_id)
            self.needle_slots.clear(target_session_id)

    def release_ownership(self) -> None:
        """Release session ownership lock in ledger."""
        self.ledger.release_ownership()

    def close(self) -> None:
        """Clean up active session state during teardown."""
        self.needle_slots.clear()
        if self.context.enabled and self.session_id is not None:
            self.context.clear_session(self.session_id)
