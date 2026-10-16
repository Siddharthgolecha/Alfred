"""Text and optional speech-synthesis feedback channel for Alfred.

TTS is opt-in; the default path only prints to stdout.
Supported TTS tools: spd-say (Linux), say (macOS).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import base64
import threading
from .constants import Truncation
from .platform_utils import IS_MACOS, IS_WINDOWS

LOG = logging.getLogger(__name__)

# Prefer the platform-native TTS tool; fall back to spd-say on Linux.
_TTS_COMMAND: list[str] | None = None


def _tts_argv(message: str) -> list[str] | None:
    """Return the TTS command argv for the current platform, or None if unavailable."""
    if IS_WINDOWS:
        # Pass speech as base64 data; never interpolate model/user text into PowerShell code.
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Speak([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" +
            base64.b64encode(str(message)[:Truncation.MESSAGE].encode("utf-8")).decode("ascii") +
            "'))); $s.Dispose()"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return ["powershell", "-NoProfile", "-EncodedCommand", encoded]
    if IS_MACOS:
        if shutil.which("say"):
            return ["say", "--", str(message)[:Truncation.MESSAGE]]
        return None
    # Linux (and other POSIX): use speech-dispatcher.
    if shutil.which("spd-say"):
        return ["spd-say", "-w", str(message)[:Truncation.MESSAGE]]
    return None


class Feedback:
    """Deliver a response message to stdout, and optionally via TTS."""

    def __init__(self, enable_tts: bool = False, processes=None) -> None:
        self.tts = enable_tts
        self.processes = processes
        self.speaking = threading.Event()

    def tell(self, message: str) -> None:
        print(message, flush=True)
        if not self.tts:
            return
        argv = _tts_argv(message)
        if argv is None:
            return
        self.speaking.set()
        try:
            runner = self.processes.run if self.processes else subprocess.run
            runner(argv, timeout=25, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("Speech output failed: %s", exc)
        finally:
            self.speaking.clear()
