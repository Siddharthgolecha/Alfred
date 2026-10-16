"""Unit tests for WakeWordListener (alfred.listener) following F.I.R.S.T. principles."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alfred.audio import AudioUnavailableError
from alfred.config import load_config
from alfred.inference_admission import InferenceAdmission
from alfred.listener import WakeWordListener
from alfred.runtime import Store
from alfred.session import SessionManager


class WakeWordListenerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["runtime"]["database"] = str(Path(self.temp_dir.name) / "test.db")
        self.cfg["runtime"]["logs"] = self.temp_dir.name
        self.cfg["wake_word"]["enabled"] = True
        self.cfg["wake_word"]["mode"] = "transcript"
        self.store = Store(self.cfg["runtime"]["database"])
        self.session_manager = SessionManager(self.store, self.cfg["runtime"])
        self.audio = Mock()
        self.wake_word = Mock()
        self.router = Mock()
        self.router._command_source = "text"
        self.feedback = Mock()
        self.feedback.tts = False
        self.admission = InferenceAdmission(enabled=False)
        self.cactus = Mock()
        self.listener = WakeWordListener(
            cfg=self.cfg,
            audio=self.audio,
            wake_word=self.wake_word,
            session_manager=self.session_manager,
            router=self.router,
            feedback=self.feedback,
            admission=self.admission,
            cactus=self.cactus,
        )

    def tearDown(self):
        self.session_manager.close()
        self.store.db.close()
        self.temp_dir.cleanup()

    def test_listener_requires_cactus(self):
        self.listener.cactus = None
        with self.assertRaises(RuntimeError):
            self.listener.listen()

    def test_listener_executes_authorized_command(self):
        class SingleUtteranceStream:
            def __init__(self, *args, **kwargs):
                pass

            def utterances(self):
                return iter([[b"audio_chunk"]])

        self.wake_word.is_armed.return_value = True
        self.cactus.transcribe_stream.return_value = "Alfred status"
        self.wake_word.consume.return_value = "status"

        with patch("alfred.listener.UtteranceStream", SingleUtteranceStream):
            self.listener.listen()

        self.router.handle_text.assert_called_once_with("status")
        self.assertEqual(self.router._command_source, "text")

    def test_listener_handles_shutdown_command(self):
        class SingleUtteranceStream:
            def __init__(self, *args, **kwargs):
                pass

            def utterances(self):
                return iter([[b"audio_chunk"]])

        self.wake_word.is_armed.return_value = True
        self.cactus.transcribe_stream.return_value = "Alfred shutdown"
        self.wake_word.consume.return_value = "shutdown"

        with patch("alfred.listener.UtteranceStream", SingleUtteranceStream):
            self.listener.listen()

        tasks = self.session_manager.ledger.list_tasks(self.session_manager.session_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["state"], "completed")

    def test_listener_discards_unauthorized_utterance(self):
        class SingleUtteranceStream:
            def __init__(self, *args, **kwargs):
                pass

            def utterances(self):
                return iter([[b"audio_chunk"]])

        self.wake_word.is_armed.return_value = False
        self.cactus.transcribe_stream.return_value = "random noise"
        self.wake_word.consume.return_value = None

        with patch("alfred.listener.UtteranceStream", SingleUtteranceStream):
            self.listener.listen()

        self.router.handle_text.assert_not_called()

    def test_listener_handles_wake_word_only_without_command(self):
        class SingleUtteranceStream:
            def __init__(self, *args, **kwargs):
                pass

            def utterances(self):
                return iter([[b"audio_chunk"]])

        self.wake_word.is_armed.return_value = True
        self.cactus.transcribe_stream.return_value = "Alfred"
        self.wake_word.consume.return_value = ""

        with patch("alfred.listener.UtteranceStream", SingleUtteranceStream):
            self.listener.listen()

        self.router.handle_text.assert_not_called()

    def test_listener_recovers_from_transcription_error(self):
        class TwoUtteranceStream:
            def __init__(self, *args, **kwargs):
                pass

            def utterances(self):
                return iter([[b"bad_chunk"], [b"good_chunk"]])

        self.wake_word.is_armed.return_value = True
        self.cactus.transcribe_stream.side_effect = [
            RuntimeError("transcription failure"),
            "Alfred status",
        ]
        self.wake_word.consume.return_value = "status"

        with patch("alfred.listener.UtteranceStream", TwoUtteranceStream), patch(
            "time.sleep", return_value=None
        ):
            self.listener.listen()

        self.router.handle_text.assert_called_once_with("status")

    def test_listener_raises_on_audio_unavailable_error(self):
        class FailingUtteranceStream:
            def __init__(self, *args, **kwargs):
                pass

            def utterances(self):
                raise AudioUnavailableError("mic unplugged")

        with patch("alfred.listener.UtteranceStream", FailingUtteranceStream):
            with self.assertRaises(AudioUnavailableError):
                self.listener.listen()


if __name__ == "__main__":
    unittest.main()
