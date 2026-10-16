"""Unit tests for Automations and tick helpers (alfred.automations) following F.I.R.S.T. principles."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from alfred.automations import Automations
from alfred.config import load_config
from alfred.runtime import Store


class AutomationsTickHelpersTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["runtime"]["database"] = str(Path(self.temp_dir.name) / "test.db")
        self.store = Store(self.cfg["runtime"]["database"])
        self.automations = Automations(self.store)
        self.tools = Mock()

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    def test_check_reminder_jobs_enqueues_due_once_reminder(self):
        now = 1800000000
        job = self.automations.remind_at("Take medication", "2030-10-15T09:00:00Z")
        job_id = job["id"]

        # Job is due at 1918208400; let's simulate now >= job due_at
        due_at = self.store.db.execute(
            "SELECT next_at FROM automation_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]

        watches = self.automations._check_reminder_jobs(due_at + 1)
        self.assertEqual(watches, [])

        # Check job is completed
        status = self.store.db.execute(
            "SELECT status FROM automation_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        self.assertEqual(status, "completed")

        # Check outbox has the message
        outbox = self.store.db.execute("SELECT * FROM automation_outbox").fetchall()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["message"], "Take medication")

    def test_check_reminder_jobs_advances_due_daily_reminder(self):
        now = 1800000000
        job = self.automations.remind_daily("Standup", "09:00", "UTC")
        job_id = job["id"]

        due_at = self.store.db.execute(
            "SELECT next_at FROM automation_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]

        watches = self.automations._check_reminder_jobs(due_at + 1)
        self.assertEqual(watches, [])

        # Job is still active, with advanced next_at
        row = self.store.db.execute(
            "SELECT status, next_at FROM automation_jobs WHERE id=?", (job_id,)
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertGreater(row["next_at"], due_at)

        # Message is enqueued
        outbox = self.store.db.execute("SELECT * FROM automation_outbox").fetchall()
        self.assertEqual(len(outbox), 1)

    def test_check_reminder_jobs_returns_due_gpu_watches(self):
        job = self.automations.watch_gpu(threshold_c=75, interval_minutes=5, cooldown_minutes=30)
        job_id = job["id"]

        due_at = self.store.db.execute(
            "SELECT next_at FROM automation_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]

        watches = self.automations._check_reminder_jobs(due_at + 1)
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0][0]["id"], job_id)
        self.assertEqual(watches[0][1]["threshold_c"], 75)

    def test_check_watch_conditions_triggers_alert_when_armed_and_hot(self):
        now = 1800000000
        job = self.automations.watch_gpu(threshold_c=75, interval_minutes=5, cooldown_minutes=30)
        job_id = job["id"]

        # Ensure watch is armed
        self.store.db.execute("UPDATE automation_jobs SET armed=1 WHERE id=?", (job_id,))

        watches = [(dict(self.store.db.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()),
                    {"threshold_c": 75, "hysteresis_c": 3, "cooldown_minutes": 30})]

        self.tools.execute.return_value = {"gpu_temp_c": 80.0}
        self.automations._check_watch_conditions(self.tools, watches, now)

        # An alert should be enqueued
        outbox = self.store.db.execute("SELECT * FROM automation_outbox").fetchall()
        self.assertEqual(len(outbox), 1)
        self.assertIn("GPU temperature alert: 80 C is at or above 75 C", outbox[0]["message"])

    def test_check_watch_conditions_arms_when_cool(self):
        now = 1800000000
        job = self.automations.watch_gpu(threshold_c=75, interval_minutes=5, cooldown_minutes=30)
        job_id = job["id"]
        self.store.db.execute("UPDATE automation_jobs SET armed=0 WHERE id=?", (job_id,))

        watches = [(dict(self.store.db.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()),
                    {"threshold_c": 75, "hysteresis_c": 3, "cooldown_minutes": 30})]

        self.tools.execute.return_value = {"gpu_temp_c": 70.0}  # <= 75 - 3 = 72
        self.automations._check_watch_conditions(self.tools, watches, now)

        armed = self.store.db.execute("SELECT armed FROM automation_jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(armed, 1)

    def test_deliver_notifications_successful_delivery(self):
        now = 1800000000
        self.automations._enqueue(1, now, "Test notification", now)

        callback = Mock()
        self.automations._deliver_notifications(callback, now)

        callback.assert_called_once()
        row = self.store.db.execute("SELECT status FROM automation_outbox").fetchone()
        self.assertEqual(row["status"], "delivered")

    def test_deliver_notifications_retries_on_error(self):
        now = 1800000000
        self.automations._enqueue(1, now, "Test notification", now)

        callback = Mock(side_effect=RuntimeError("TTS down"))
        self.automations._deliver_notifications(callback, now)

        row = self.store.db.execute("SELECT status, retry_at FROM automation_outbox").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertGreater(row["retry_at"], now)


if __name__ == "__main__":
    unittest.main()
