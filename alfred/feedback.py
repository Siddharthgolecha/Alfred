"""Text and optional speech-synthesis feedback channel for Alfred.

TTS is opt-in; the default path only prints to stdout.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Optional

from .constants import Truncation

LOG = logging.getLogger(__name__)


class Feedback:
    """Deliver a response message to stdout, and optionally via TTS."""

    def __init__(self, enable_tts: bool = False, processes=None) -> None:
        self.tts = enable_tts
        self.processes = processes
        self.speaking = threading.Event()

    def tell(self, message: str) -> None:
        print(message, flush=True)
        if self.tts and shutil.which("spd-say"):
            self.speaking.set()
            try:
                runner = self.processes.run if self.processes else subprocess.run
                runner(["spd-say", "-w", str(message)[:Truncation.MESSAGE]],
                       timeout=25, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                LOG.warning("Speech output failed: %s", exc)
            finally:
                self.speaking.clear()
