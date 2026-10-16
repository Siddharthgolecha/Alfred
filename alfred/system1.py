"""Swappable System 1 fast-path routing engine protocol and Needle 3 implementation."""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from .system import System

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


class System1(System):
    """Needle 3 router engine wrapping native needle.Needle with admission gating."""

    def __init__(self, schemas: list[dict[str, Any]], admission: Any | None = None) -> None:
        self.schemas = schemas
        super().__init__(admission)
        import needle  # type: ignore[import-untyped]

        self.needle = needle
        self.agent = needle.Needle(tools=schemas)

    def route(self, transcript: str) -> dict[str, Any]:
        """Perform initial routing step under 'needle' admission claim."""
        with self.inference_claim("needle"):
            self.agent.reset()
            return self.agent.complete(transcript, max_new_tokens=MAX_NEW_TOKENS)

    def route_feedback(self, observation_json: str) -> dict[str, Any]:
        """Continue task loop without reset under 'needle_feedback' admission claim."""
        with self.inference_claim("needle_feedback"):
            return self.agent.complete(observation_json, max_new_tokens=MAX_NEW_TOKENS)


# Preserve the pre-hierarchy public name for callers that construct the
# concrete Needle engine directly.
Needle3Engine = System1
