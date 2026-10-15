"""Wake-phrase and optional on-device acoustic activation for voice commands.

The default transcript gate needs no additional dependency; unaddressed speech
can be transcribed locally but never reaches Needle, Qwen, or tool execution.
An optional openWakeWord detector can gate Whistle itself when a matching
custom wake model is installed.
"""
from __future__ import annotations

from collections import deque
import itertools
import math
from pathlib import Path
import re
import time


class WakeWordGate:
    """Only explicit wake-prefixed speech or one timely follow-up is actionable."""

    def __init__(self, config):
        self.enabled = config["enabled"]
        self.phrase = config["phrase"].strip()
        self.mode = config["mode"]
        self.followup_seconds = float(config["followup_seconds"])
        # The phrase is literal: no regex injection, substrings or wake word
        # embedded inside unrelated speech. "Hey Alfred" is also accepted.
        words = r"\s+".join(re.escape(part) for part in self.phrase.split())
        self._prefix = re.compile(
            r"^\s*(?:(?:hey|hi|hello|ok|okay)[,\s]+)?" + words +
            r"(?=$|[\s,.:;!?\-\u2014])", re.IGNORECASE
        )
        self._armed_until = 0.0

    def is_armed(self, now=None):
        instant = time.monotonic() if now is None else now
        return self.enabled and self._armed_until > instant

    def _command_after_prefix(self, text):
        match = self._prefix.match(text)
        if not match:
            return None
        return text[match.end():].lstrip(" \t\r\n,.:;!?-\u2014").strip()

    def preview(self, text, *, acoustic_activation=False, now=None):
        """Return a displayable hypothesis only when it could be actionable."""
        text = str(text or "").strip()
        if not text:
            return None
        if not self.enabled or self.is_armed(now) or acoustic_activation:
            return text
        return text if self._command_after_prefix(text) is not None else None

    def consume(self, text, *, acoustic_activation=False, now=None):
        """Return stripped command, '' for a wake-only event, None if ignored.

        Once armed, a single nonempty utterance consumes the window. A lone
        wake word renews the window instead; no perpetual open microphone.
        """
        instant = time.monotonic() if now is None else now
        text = str(text or "").strip()
        if not self.enabled:
            return text or None
        command = self._command_after_prefix(text) if text else None
        if command is not None:
            self._armed_until = 0.0
            if command:
                return command
            self._armed_until = instant + self.followup_seconds
            return ""
        allowed = acoustic_activation or self.is_armed(instant)
        self._armed_until = 0.0
        if not allowed:
            return None
        if not text:
            # Acoustic detection succeeded, but the recognizer may have
            # emitted no finalized text for the wake-only utterance.
            if acoustic_activation:
                self._armed_until = instant + self.followup_seconds
                return ""
            return None
        return text


class AcousticWakeDetector:
    """Optional pre-STT gate for a local openWakeWord ONNX wake model.

    AudioManager already emits 16 kHz mono float PCM. Accumulate exact
    80 ms/1280-sample frames irrespective of the configured capture frame size.
    Import the heavy optional dependency only when this mode is selected.
    """

    WINDOW_SAMPLES = 1280

    def __init__(self, config):
        path = Path(config["model_path"]).expanduser()
        if not path.is_file():
            raise RuntimeError(
                "wake_word.mode=openwakeword requires a local custom model at "
                "wake_word.model_path (trained for the configured wake phrase)"
            )
        try:
            import numpy as np  # type: ignore[import-not-found]
            from openwakeword.model import Model  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Acoustic wake detection requires: pip install '.[wakeword]' "
                "(and a matching wake_word.model_path)"
            ) from exc
        self.np = np
        self.model = Model(wakeword_models=[str(path)], inference_framework="onnx")
        self.threshold = float(config["threshold"])
        self._pending = np.empty(0, dtype=np.int16)

    def reset(self):
        self._pending = self.np.empty(0, dtype=self.np.int16)
        reset = getattr(self.model, "reset", None)
        if callable(reset):
            reset()

    def feed(self, samples):
        np = self.np
        pcm = np.asarray(samples, dtype=np.float32)
        if pcm.ndim != 1:
            raise ValueError("Wake audio must be mono PCM")
        pcm = np.nan_to_num(pcm, nan=0.0, posinf=0.0, neginf=0.0)
        converted = np.rint(np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
        self._pending = np.concatenate((self._pending, converted))
        hit = False
        while len(self._pending) >= self.WINDOW_SAMPLES:
            frame = self._pending[:self.WINDOW_SAMPLES]
            self._pending = self._pending[self.WINDOW_SAMPLES:]
            scores = self.model.predict(frame)
            if isinstance(scores, dict):
                for score in scores.values():
                    try:
                        value = float(score)  # ONNX may return numpy.float32.
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value) and value >= self.threshold:
                        hit = True
        return hit

    def capture_after_wake(self, chunks, frame_ms):
        """Consume unaddressed audio without STT; return replay + live tail.

        A short pre-roll lets Whistle hear the phrase and immediate command
        even when the acoustic detector activates halfway through speech.
        """
        history = deque(maxlen=max(2, math.ceil(2000 / float(frame_ms))))
        for chunk in chunks:
            history.append(chunk)
            if self.feed(chunk):
                self.reset()
                return itertools.chain(tuple(history), chunks)
        return None
