"""Swappable Speech-To-Text (STT) engine protocol and Whistle STT implementation."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import nullcontext
import logging
from typing import Any, Protocol, runtime_checkable

LOG = logging.getLogger(__name__)


@runtime_checkable
class STTEngineProtocol(Protocol):
    """Protocol for streaming Speech-to-Text inference engines."""

    def transcribe_stream(
        self,
        frames: Iterable[bytes] | Any,
        on_partial: Callable[[str], None] | None = None,
    ) -> str:
        """Stream audio frames and return the final transcribed text."""
        ...


class WhistleSTTEngine:
    """Whistle STT engine wrapping native needle.Whistle with admission gating."""

    DEFAULT_KEYWORDS = ("Alfred", "Jetson", "Sony")

    def __init__(
        self,
        admission: Any | None = None,
        keywords: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.admission = admission
        self.keywords = list(keywords or self.DEFAULT_KEYWORDS)
        import needle  # type: ignore[import-untyped]

        self.whistle = needle.Whistle()

    def transcribe_stream(
        self,
        frames: Iterable[bytes] | Any,
        on_partial: Callable[[str], None] | None = None,
    ) -> str:
        """Transcribe streaming audio frames under admission claim."""
        guard = self.admission.claim("whistle") if self.admission else nullcontext()
        with guard:
            return self._transcribe_admitted(frames, on_partial)

    def _transcribe_admitted(
        self,
        frames: Iterable[bytes] | Any,
        on_partial: Callable[[str], None] | None = None,
    ) -> str:
        confirmed_text: list[str] = []
        pending = ""
        for event in self.whistle.stream(frames, language="en", keywords=self.keywords):
            committed = str(event.get("text") or "").strip()
            pending = str(event.get("pending") or "")
            if committed:
                confirmed_text.append(committed)
            if on_partial:
                confirmed = " ".join(confirmed_text).strip()
                provisional = pending.strip()
                if provisional and confirmed and provisional.lower().startswith(confirmed.lower()):
                    shown = provisional
                else:
                    shown = " ".join(part for part in (confirmed, provisional) if part)
                on_partial(shown)
        return " ".join(confirmed_text).strip()
