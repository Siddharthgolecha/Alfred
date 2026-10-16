"""Swappable System 1 fast-path routing engine protocol and Needle 3 implementation."""
from __future__ import annotations

from contextlib import nullcontext
import logging
from typing import Any, Protocol, runtime_checkable

LOG = logging.getLogger(__name__)
MAX_NEW_TOKENS = 128


@runtime_checkable
class System1RouterProtocol(Protocol):
    """Protocol for fast-path reactive tool routing engines."""

    def route(self, transcript: str) -> dict[str, Any]:
        """Route user transcript to candidate tool calls."""
        ...

    def route_feedback(self, observation_json: str) -> dict[str, Any]:
        """Continue multi-turn routing given tool observation JSON."""
        ...


class Needle3Engine:
    """Needle 3 router engine wrapping native needle.Needle with admission gating."""

    def __init__(self, schemas: list[dict[str, Any]], admission: Any | None = None) -> None:
        self.schemas = schemas
        self.admission = admission
        import needle  # type: ignore[import-untyped]

        self.needle = needle
        self.agent = needle.Needle(tools=schemas)

    def route(self, transcript: str) -> dict[str, Any]:
        """Perform initial routing step under 'needle' admission claim."""
        guard = self.admission.claim("needle") if self.admission else nullcontext()
        with guard:
            self.agent.reset()
            return self.agent.complete(transcript, max_new_tokens=MAX_NEW_TOKENS)

    def route_feedback(self, observation_json: str) -> dict[str, Any]:
        """Continue task loop without reset under 'needle_feedback' admission claim."""
        guard = self.admission.claim("needle_feedback") if self.admission else nullcontext()
        with guard:
            return self.agent.complete(observation_json, max_new_tokens=MAX_NEW_TOKENS)
