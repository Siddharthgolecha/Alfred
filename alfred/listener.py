"""Wake word listener loop and audio input orchestration."""
from __future__ import annotations

import logging
import re
import sys
import time
from typing import Any, Callable

import alfred.audio as _audio
import alfred.wakeword as _wakeword
from .audio import AudioManager, AudioUnavailableError, UtteranceStream
from .cognitive import Completion, Decision
from .console import TranscriptDisplay
from .feedback import Feedback
from .inference_admission import InferenceAdmission
from .models import Cactus
from .router import RequestRouter
from .session import SessionManager
from .wakeword import AcousticWakeDetector, WakeWordGate

LOG = logging.getLogger(__name__)


class WakeWordListener:
    """Manages the wake-word detection loop and hands authorized utterances to routing."""

    def __init__(
        self,
        cfg: dict[str, Any],
        audio: AudioManager,
        wake_word: WakeWordGate,
        session_manager: SessionManager,
        router: RequestRouter,
        feedback: Feedback,
        admission: InferenceAdmission,
        cactus: Cactus | None = None,
        handle_text_fn: Callable[[str], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.audio = audio
        self.wake_word = wake_word
        self.session_manager = session_manager
        self.router = router
        self.feedback = feedback
        self.admission = admission
        self.cactus = cactus
        self.handle_text_fn = handle_text_fn or router.handle_text

    def listen(self) -> None:
        """Main audio listening loop for wake-word activation and command execution."""
        if self.cactus is None:
            raise RuntimeError("Cactus models not loaded")

        # Runtime lookup of the already-imported `alfred.app` module (not a static
        # import, which would be circular: app imports this listener). Tests such as
        # tests/test_wakeword.py and tests/test_voice_routing.py patch
        # `alfred.app.UtteranceStream` / `alfred.app.AcousticWakeDetector`; since this
        # module bound its own references at import time, it must consult the app
        # module's current attributes and prefer them when they differ from the
        # originals in alfred.audio / alfred.wakeword.
        app_module = sys.modules.get("alfred.app")
        utterance_stream_cls = UtteranceStream
        if (
            app_module
            and hasattr(app_module, "UtteranceStream")
            and app_module.UtteranceStream is not _audio.UtteranceStream
        ):
            utterance_stream_cls = app_module.UtteranceStream

        acoustic_wake_detector_cls = AcousticWakeDetector
        if (
            app_module
            and hasattr(app_module, "AcousticWakeDetector")
            and app_module.AcousticWakeDetector is not _wakeword.AcousticWakeDetector
        ):
            acoustic_wake_detector_cls = app_module.AcousticWakeDetector

        wake_cfg = self.cfg["wake_word"]
        detector = (
            acoustic_wake_detector_cls(wake_cfg)
            if wake_cfg["enabled"] and wake_cfg["mode"] == "openwakeword"
            else None
        )
        LOG.info(
            "Starting voice capture; wake word %s (%s)",
            "enabled" if wake_cfg["enabled"] else "disabled",
            wake_cfg["mode"],
        )
        for chunks in utterance_stream_cls(self.audio.frames(), self.cfg["audio"]).utterances():
            transcript_display = TranscriptDisplay()
            try:
                acoustically_activated = False
                if detector is not None and not self.wake_word.is_armed():
                    chunks = detector.capture_after_wake(chunks, self.cfg["audio"]["frame_ms"])
                    if chunks is None:
                        continue
                    acoustically_activated = True

                def show_partial(hypothesis: str) -> None:
                    if (
                        self.wake_word.preview(
                            hypothesis, acoustic_activation=acoustically_activated
                        )
                        is not None
                    ):
                        transcript_display.update(hypothesis)

                self.admission.set_capture_active(True)
                try:
                    transcript = self.cactus.transcribe_stream(chunks, on_partial=show_partial)
                finally:
                    self.admission.set_capture_active(False)

                command = self.wake_word.consume(
                    transcript, acoustic_activation=acoustically_activated
                )
                if command is None:
                    transcript_display.discard()
                    continue

                transcript_display.finish(transcript or wake_cfg["phrase"])
                if command:
                    if re.fullmatch(
                        r"(?:close|exit|quit|stop listening|shut down|shutdown)[.!?]*",
                        command.strip(),
                        flags=re.IGNORECASE,
                    ):
                        task = self.session_manager.start_task("voice", len(command))
                        self.session_manager.sessions.append(
                            task.task_id,
                            Decision(engine="local", route="shutdown", call_count=0),
                        )
                        self.session_manager.sessions.append(
                            task.task_id, Completion(outcome="completed", reason="shutdown")
                        )
                        LOG.info("Wake-authorized spoken shutdown")
                        print("Stopping Alfred", flush=True)
                        break

                    LOG.info("Wake-authorized command received (%d characters)", len(command))
                    previous_source = self.router._command_source
                    self.router._command_source = "voice"
                    try:
                        self.handle_text_fn(command)
                    finally:
                        self.router._command_source = previous_source
                else:
                    LOG.info("Wake word detected; awaiting follow-up command")

                if self.feedback.tts:
                    self.audio.close()
            except (KeyboardInterrupt, AudioUnavailableError):
                transcript_display.abort()
                raise
            except Exception as listener_error:
                transcript_display.abort()
                LOG.exception("Audio/transcription error, recovering: %s", listener_error)
                self.audio.close()
                if detector is not None:
                    detector.reset()
                time.sleep(1)
