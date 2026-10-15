"""Unit tests for SessionManager (alfred.session) following F.I.R.S.T. principles."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from alfred.config import load_config
from alfred.runtime import Store
from alfred.session import SessionManager


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["runtime"]["database"] = str(Path(self.temp_dir.name) / "test.db")
        self.store = Store(self.cfg["runtime"]["database"])
        self.session_manager = SessionManager(self.store, self.cfg["runtime"])

    def tearDown(self):
        self.session_manager.close()
        self.store.db.close()
        self.temp_dir.cleanup()

    def test_init_sets_up_ledger_slots_and_context(self):
        self.assertIsNone(self.session_manager.session_id)
        self.assertIsNone(self.session_manager.active_task_id)
        self.assertIsNotNone(self.session_manager.ledger)
        self.assertIs(self.session_manager.sessions, self.session_manager.ledger)
        self.assertIsNotNone(self.session_manager.needle_slots)
        self.assertIsNotNone(self.session_manager.context)

    def test_start_task_generates_session_and_task_id(self):
        envelope = self.session_manager.start_task("text", 15)
        self.assertIsNotNone(envelope.session_id)
        self.assertIsNotNone(envelope.task_id)
        self.assertEqual(self.session_manager.session_id, envelope.session_id)
        self.assertEqual(self.session_manager.active_task_id, envelope.task_id)

    def test_start_subsequent_task_reuses_session_id(self):
        first_envelope = self.session_manager.start_task("text", 10)
        first_session = first_envelope.session_id
        second_envelope = self.session_manager.start_task("text", 20)
        self.assertEqual(second_envelope.session_id, first_session)
        self.assertNotEqual(second_envelope.task_id, first_envelope.task_id)

    def test_clear_active_task_resets_task_id(self):
        self.session_manager.start_task("text", 12)
        self.assertIsNotNone(self.session_manager.active_task_id)
        self.session_manager.clear_active_task()
        self.assertIsNone(self.session_manager.active_task_id)

    def test_resume_session_restores_session_id(self):
        envelope = self.session_manager.start_task("text", 5)
        original_session = envelope.session_id
        self.session_manager.release_ownership()

        new_manager = SessionManager(self.store, self.cfg["runtime"])
        resumed = new_manager.resume_session(original_session)
        self.assertEqual(resumed, original_session)
        self.assertEqual(new_manager.session_id, original_session)
        new_manager.release_ownership()

    def test_resume_session_raises_when_session_already_active(self):
        self.session_manager.start_task("text", 5)
        with self.assertRaises(ValueError):
            self.session_manager.resume_session("another_session")

    def test_clear_session_resets_context_and_slots_for_current_session(self):
        envelope = self.session_manager.start_task("text", 8)
        self.session_manager.needle_slots.remember(
            envelope.session_id, "check gpu", "get_gpu_status", {"gpu_temp_c": 55}
        )
        self.session_manager.clear_session()
        resolved = self.session_manager.needle_slots.resolve(
            envelope.session_id, "how hot is the gpu"
        )
        self.assertEqual(resolved, "how hot is the gpu")

    def test_clear_session_with_explicit_session_and_task_id(self):
        mock_context = Mock()
        mock_slots = Mock()
        self.session_manager.context = mock_context
        self.session_manager.needle_slots = mock_slots

        self.session_manager.clear_session("sess-123", task_id="task-456")
        mock_context.clear_session.assert_called_once_with("sess-123", task_id="task-456")
        mock_slots.clear.assert_called_once_with("sess-123")

    def test_clear_session_when_no_session_is_noop(self):
        self.assertIsNone(self.session_manager.session_id)
        # Should not raise
        self.session_manager.clear_session()

    def test_release_ownership_delegates_to_ledger(self):
        self.session_manager.start_task("text", 5)
        # Should not raise and should release ownership
        self.session_manager.release_ownership()

    def test_close_cleans_up_slots_and_context(self):
        mock_context = Mock()
        mock_slots = Mock()
        self.session_manager.context = mock_context
        self.session_manager.needle_slots = mock_slots
        self.session_manager.session_id = "sess-789"

        self.session_manager.close()
        mock_slots.clear.assert_called_once_with()
        mock_context.clear_session.assert_called_once_with("sess-789")


if __name__ == "__main__":
    unittest.main()
