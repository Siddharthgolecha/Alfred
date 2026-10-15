"""Unit tests for RequestRouter and routing helpers (alfred.router) following F.I.R.S.T. principles."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alfred.cognitive.harness_sdk import HarnessUnavailable
from alfred.config import load_config
from alfred.feedback import Feedback
from alfred.inference_admission import InferenceAdmission
from alfred.lifecycle import ChildProcesses
from alfred.models import LlamaSupervisor, Qwen
from alfred.router import (
    RequestRouter,
    adjust_status_calls,
    explicit_gpu_status_request,
)
from alfred.runtime import Store, Tools
from alfred.session import SessionManager
from alfred.system2 import System2


class RequestRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["runtime"]["database"] = str(Path(self.temp_dir.name) / "test.db")
        self.cfg["runtime"]["logs"] = self.temp_dir.name
        self.cfg["runtime"]["resource_admission_enabled"] = False
        self.store = Store(self.cfg["runtime"]["database"])
        self.session_manager = SessionManager(self.store, self.cfg["runtime"])
        self.processes = ChildProcesses()
        self.admission = InferenceAdmission(enabled=False)
        self.tools = Tools(self.cfg["runtime"], self.store, processes=self.processes)
        self.feedback = Feedback(self.cfg["runtime"]["tts"], processes=self.processes)
        self.feedback.tell = Mock()
        self.supervisor = LlamaSupervisor(self.cfg["qwen"], self.cfg["runtime"]["logs"])
        self.supervisor.ensure_running = Mock()
        self.qwen = Qwen(self.cfg["qwen"], self.supervisor)
        self.system2 = System2(self.qwen, self.tools, self.store, self.cfg["runtime"])
        self.harness = Mock()
        self.cactus = Mock()
        self.router = RequestRouter(
            cfg=self.cfg,
            store=self.store,
            session_manager=self.session_manager,
            system2=self.system2,
            supervisor=self.supervisor,
            harness=self.harness,
            admission=self.admission,
            feedback=self.feedback,
            tools=self.tools,
            cactus=self.cactus,
        )

    def tearDown(self):
        self.session_manager.close()
        self.store.db.close()
        self.temp_dir.cleanup()

    def test_explicit_gpu_status_request_positive(self):
        self.assertTrue(explicit_gpu_status_request("what is the gpu temperature"))
        self.assertTrue(explicit_gpu_status_request("gpu status"))
        self.assertTrue(explicit_gpu_status_request("show graphics processor metrics"))
        self.assertTrue(explicit_gpu_status_request("graphics card temp"))

    def test_explicit_gpu_status_request_negative(self):
        self.assertFalse(explicit_gpu_status_request("why is the gpu temperature high"))
        self.assertFalse(explicit_gpu_status_request("turn off the computer"))
        self.assertFalse(explicit_gpu_status_request("explain gpu usage"))

    def test_adjust_status_calls_rewrites_to_gpu_when_explicit(self):
        calls = [{"name": "get_system_status", "arguments": {}}]
        adjusted = adjust_status_calls("what is the gpu temperature", calls)
        self.assertEqual(len(adjusted), 1)
        self.assertEqual(adjusted[0]["name"], "get_gpu_status")

    def test_adjust_status_calls_leaves_non_gpu_unchanged(self):
        calls = [{"name": "get_system_status", "arguments": {}}]
        adjusted = adjust_status_calls("what is the system status", calls)
        self.assertEqual(len(adjusted), 1)
        self.assertEqual(adjusted[0]["name"], "get_system_status")

    def test_needle_task_lock_property_returns_lock(self):
        self.assertIs(self.router._needle_task_lock, self.router._needle_execution_lock)

    def test_handle_text_empty_input_is_ignored(self):
        self.router.handle_text("   ")
        self.assertIsNone(self.session_manager.session_id)

    def test_handle_text_direct_route_executes_plan(self):
        self.cactus.route.return_value = {
            "confidence": 0.95,
            "function_calls": [{"name": "get_gpu_status", "arguments": {}}],
        }
        self.system2.direct = Mock(
            return_value={"message": "ok", "results": [{"gpu_temp_c": 50}]}
        )
        self.router.handle_text("gpu temperature")
        self.system2.direct.assert_called_once()
        tasks = self.session_manager.ledger.list_tasks(self.session_manager.session_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["state"], "completed")

    def test_reason_raises_when_no_active_session(self):
        self.assertIsNone(self.session_manager.session_id)
        with self.assertRaises(HarnessUnavailable):
            self.router.reason("explain something")

    def test_reason_raises_when_qwen_disabled(self):
        envelope = self.session_manager.start_task("text", 10)
        self.cfg["qwen"]["enabled"] = False
        with self.assertRaises(HarnessUnavailable):
            self.router.reason("explain something")

    def test_reason_readonly_calls_harness_run(self):
        self.session_manager.start_task("text", 10)
        self.harness.run.return_value = {"message": "Here is the explanation"}
        result = self.router.reason("explain memory", allow_actions=False)
        self.harness.run.assert_called_once()
        self.assertEqual(result["message"], "Here is the explanation")

    def test_reason_with_actions_plans_and_executes(self):
        self.session_manager.start_task("text", 10)
        self.harness.plan.return_value = {
            "message": "Let me check",
            "steps": [{"tool": "get_gpu_status", "args": {}}],
        }
        self.system2.execute_or_stage = Mock(return_value={"message": "executed", "results": []})
        result = self.router.reason("check gpu status", allow_actions=True)
        self.harness.plan.assert_called_once()
        self.system2.execute_or_stage.assert_called_once()

    def test_handoff_records_escalation_and_delegates_to_system2(self):
        task = self.session_manager.start_task("text", 10)
        self.system2.continue_handoff = Mock(return_value={"message": "Handoff done"})
        result = self.router._handoff(task, "original command", "delegated")
        self.system2.continue_handoff.assert_called_once()
        self.assertEqual(result["message"], "Handoff done")

    def test_observe_needle_prefix_records_telemetry(self):
        task = self.session_manager.start_task("text", 10)
        from alfred.cognitive.needle_loop import LoopOutcome
        outcome = LoopOutcome(
            result={"results": [{"gpu_temp_c": 50}]},
            accepted_calls=({"name": "get_gpu_status", "arguments": {}},),
            stop="complete",
            observed=("get_gpu_status",),
        )
        # Should record event without raising
        self.router._observe_needle_prefix(task, "prefix text", outcome)


if __name__ == "__main__":
    unittest.main()
