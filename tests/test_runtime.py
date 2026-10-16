"""Unit tests for Tools and runtime handlers (alfred.runtime) following F.I.R.S.T. principles."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alfred.config import load_config
from alfred.runtime import Store, Tools


class RuntimeToolsHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["runtime"]["database"] = str(Path(self.temp_dir.name) / "test.db")
        self.cfg["runtime"]["allowed_services"] = ["test-service"]
        self.cfg["runtime"]["named_tasks"] = {"test-task": ["echo", "hello"]}
        self.store = Store(self.cfg["runtime"]["database"])
        self.processes = Mock()
        self.tools = Tools(self.cfg["runtime"], self.store, processes=self.processes)

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    def test_handle_get_system_status(self):
        result = self.tools._handle_get_system_status({})
        self.assertIn("load_1_5_15", result)
        self.assertIn("memory_mib", result)
        self.assertIn("root_free_gib", result)

    def test_handle_get_gpu_status(self):
        result = self.tools._handle_get_gpu_status({})
        self.assertIn("available", result)

    def test_handle_list_services(self):
        self.tools._service = Mock(return_value="active")
        result = self.tools._handle_list_services({})
        self.assertEqual(result, {"test-service": "active"})

    def test_handle_control_service_success(self):
        self.tools._service = Mock(return_value="started")
        result = self.tools._handle_control_service({"name": "test-service", "action": "start"})
        self.assertEqual(result, {"name": "test-service", "action": "start", "result": "started"})

    def test_handle_control_service_disallowed_raises_permission_error(self):
        with self.assertRaises(PermissionError):
            self.tools._handle_control_service({"name": "unauthorized-service", "action": "start"})

    def test_handle_run_named_task_success(self):
        mock_proc = Mock(returncode=0, stdout="hello\n", stderr="")
        self.processes.run.return_value = mock_proc
        result = self.tools._handle_run_named_task({"name": "test-task"})
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "hello\n")

    def test_handle_run_named_task_disallowed_raises_permission_error(self):
        with self.assertRaises(PermissionError):
            self.tools._handle_run_named_task({"name": "unauthorized-task"})

    def test_handle_create_reminder_success(self):
        result = self.tools._handle_create_reminder({"message": "Buy groceries", "delay_minutes": 10})
        self.assertIn("id", result)
        self.assertEqual(result["message"], "Buy groceries")

    def test_handle_create_reminder_blank_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.tools._handle_create_reminder({"message": "   ", "delay_minutes": 10})

    def test_handle_create_reminder_at(self):
        result = self.tools._handle_create_reminder_at(
            {"message": "Doctor appointment", "when": "2030-10-15T09:00:00+05:30"}
        )
        self.assertIn("id", result)

    def test_handle_create_daily_reminder(self):
        result = self.tools._handle_create_daily_reminder(
            {"message": "Morning standup", "local_time": "09:30", "timezone": "Asia/Kolkata"}
        )
        self.assertIn("id", result)

    def test_handle_watch_gpu_temperature(self):
        result = self.tools._handle_watch_gpu_temperature(
            {"threshold_c": 75, "interval_minutes": 5, "cooldown_minutes": 30}
        )
        self.assertIn("id", result)

    def test_handle_list_automations(self):
        self.tools._handle_create_reminder_at(
            {"message": "Test job", "when": "2030-10-15T09:00:00+05:30"}
        )
        jobs = self.tools._handle_list_automations({})
        self.assertEqual(len(jobs), 1)

    def test_handle_pause_and_resume_and_cancel_automation(self):
        job = self.tools._handle_create_reminder_at(
            {"message": "Test job", "when": "2030-10-15T09:00:00+05:30"}
        )
        job_id = job["id"]

        paused = self.tools._handle_pause_automation({"automation_id": job_id})
        self.assertEqual(paused["status"], "paused")

        resumed = self.tools._handle_resume_automation({"automation_id": job_id})
        self.assertEqual(resumed["status"], "active")

        cancelled = self.tools._handle_cancel_automation({"automation_id": job_id})
        self.assertEqual(cancelled["status"], "cancelled")

    def test_handle_list_and_cancel_reminders(self):
        rem = self.tools._handle_create_reminder({"message": "Test reminder", "delay_minutes": 5})
        rems = self.tools._handle_list_reminders({})
        self.assertEqual(len(rems), 1)

        res = self.tools._handle_cancel_reminder({"reminder_id": rem["id"]})
        self.assertEqual(res["cancelled"], True)

    def test_handle_schedule_every_and_list_and_cancel(self):
        sched = self.tools._handle_schedule_every({"tool": "get_gpu_status", "interval_minutes": 15})
        self.assertIn("id", sched)

        scheds = self.tools._handle_list_schedules({})
        self.assertEqual(len(scheds), 1)

        res = self.tools._handle_cancel_schedule({"schedule_id": sched["id"]})
        self.assertEqual(res["cancelled"], True)

    def test_handle_run_and_list_workflows(self):
        self.store.save_workflow("test_flow", [{"tool": "get_gpu_status", "args": {}}])
        workflows = self.tools._handle_list_workflows({})
        self.assertEqual(len(workflows), 1)
        self.assertEqual(workflows[0]["name"], "test_flow")

        with patch.object(self.tools, "run_plan", return_value=[{"result": "ok"}]):
            res = self.tools._handle_run_workflow({"name": "test_flow"})
            self.assertEqual(res, [{"result": "ok"}])

    def test_execute_dispatches_to_registered_handler(self):
        self.tools._service = Mock(return_value="active")
        res = self.tools.execute("list_services", {})
        self.assertEqual(res, {"test-service": "active"})

    def test_execute_unknown_tool_raises_permission_error(self):
        with self.assertRaises(ValueError):  # validate() catches non-existent tools in schema first
            self.tools.execute("non_existent_tool", {})

    def test_execute_delegate_to_reasoner_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.tools.execute("delegate_to_reasoner", {"request": "hello"})


if __name__ == "__main__":
    unittest.main()
