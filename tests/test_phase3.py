"""Phase 3 persistence, timezone, permission, and outbox regression tests."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from alfred.automations import Automations, next_daily_time, parse_aware_time
from alfred.config import load_config
from alfred.runtime import Store, Tools, SCHEMA_BY_NAME
from alfred.system2 import System2


class Phase3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.file=Path(self.tmp.name)/"state.sqlite"
        self.store=Store(self.file)
        self.tools=Tools({"allowed_services":[],"named_tasks":{},"max_plan_steps":5},self.store)
        self.auto=self.tools.automations
        self.deliveries=[]

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def _due(self,id,when=None):
        when=int(time.time())-2 if when is None else when
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE automation_jobs SET next_at=? WHERE id=?",
                                  (when,id))
        return when

    def test_explicit_timezone_required(self):
        tomorrow=datetime.now(timezone.utc)+timedelta(days=1)
        self.assertGreater(parse_aware_time(tomorrow.isoformat()),int(time.time()))
        with self.assertRaisesRegex(ValueError,"offset"):
            parse_aware_time(tomorrow.strftime("%Y-%m-%dT%H:%M:%S"))
        with self.assertRaisesRegex(ValueError,"future"):
            parse_aware_time("2020-01-01T12:00:00Z")
        with self.assertRaises(ValueError):
            parse_aware_time("garbage")

    def test_absolute_reminder_delivered_once(self):
        when=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        job=self.tools.execute("create_reminder_at",{"message":"Check Jetson","when":when})
        due=self._due(job["id"])
        self.auto.tick(self.tools,self.deliveries.append)
        self.auto.tick(self.tools,self.deliveries.append)
        self.assertEqual(len(self.deliveries),1)
        self.assertEqual(self.deliveries[0]["message"],"Check Jetson")
        jobs=self.auto.list_jobs(all_states=True)
        self.assertEqual(jobs[0]["status"],"completed")

    def test_outbox_survives_restart_and_never_loses_failed_callback(self):
        when=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        job=self.auto.remind_at("Durable",when)
        self._due(job["id"])
        def broken(event):
            raise RuntimeError("notification channel unavailable")
        self.auto.tick(self.tools,broken)
        with self.store.lock,self.store.db:
            row=self.store.db.execute(
                "SELECT status,attempts FROM automation_outbox").fetchone()
        self.assertEqual(row["status"],"pending")
        self.assertEqual(row["attempts"],1)
        self.store.db.close()
        second=Store(self.file)
        new_tools=Tools({},second)
        new_auto=new_tools.automations
        with second.lock,second.db:
            second.db.execute(
                "UPDATE automation_outbox SET retry_at=? WHERE status='pending'",
                (int(time.time())-1,))
        events=[]
        new_auto.tick(new_tools,events.append)
        new_auto.tick(new_tools,events.append)
        self.assertEqual(len(events),1)
        with second.lock:
            self.assertEqual(second.db.execute(
                "SELECT status FROM automation_outbox").fetchone()["status"],"delivered")
        second.db.close()

    def test_daily_reminder_recurrence_and_no_duplicate_on_same_tick(self):
        job=self.auto.remind_daily("Morning brief","09:00","Asia/Kolkata")
        due=self._due(job["id"])
        self.auto.tick(self.tools,self.deliveries.append)
        self.auto.tick(self.tools,self.deliveries.append)
        self.assertEqual(len(self.deliveries),1)
        current=self.auto.list_jobs()[0]
        self.assertEqual(current["status"],"active")
        self.assertGreater(current["next_at"],int(time.time()))
        self.assertEqual(current["timezone"],"Asia/Kolkata")

    def test_daily_recurrence_handles_dst_and_invalid_time(self):
        now=int(datetime(2026,3,8,6,59,tzinfo=timezone.utc).timestamp())
        timestamp=next_daily_time("02:30","America/New_York",now)
        local=datetime.fromtimestamp(timestamp, __import__("zoneinfo").ZoneInfo("America/New_York"))
        self.assertEqual((local.month,local.day,local.hour,local.minute),(3,9,2,30))
        with self.assertRaises(ValueError):
            next_daily_time("25:00","Asia/Kolkata")
        with self.assertRaises(ValueError):
            next_daily_time("09:00","Not/A_Real_Zone")

    def test_cancel_discards_pending_undelivered_notification(self):
        job=self.auto.remind_daily("Silenced", "10:00", "Asia/Kolkata")
        self._due(job["id"])
        # Simulate delivery failure; outbox stores a pending retry.
        self.auto.tick(self.tools,lambda event: (_ for _ in ()).throw(RuntimeError("offline")))
        with self.store.lock:
            self.assertEqual(self.store.db.execute(
                "SELECT count(*) FROM automation_outbox WHERE status='pending'"
                ).fetchone()[0],1)
        self.assertTrue(self.auto.change(job["id"],"cancel")["changed"])
        with self.store.lock:
            self.assertEqual(self.store.db.execute(
                "SELECT count(*) FROM automation_outbox WHERE status='pending'"
                ).fetchone()[0],0)
        self.auto.tick(self.tools,self.deliveries.append)
        self.assertEqual(self.deliveries,[])

    def test_daily_pause_resume_cancel_and_list(self):
        job=self.auto.remind_daily("Tea","18:30","Asia/Kolkata")
        id=job["id"]
        self.assertTrue(self.auto.change(id,"pause")["changed"])
        self._due(id)
        self.auto.tick(self.tools,self.deliveries.append)
        self.assertEqual(self.deliveries,[])
        self.assertTrue(self.auto.change(id,"resume")["changed"])
        self.assertTrue(self.auto.change(id,"cancel")["changed"])
        self._due(id)
        self.auto.tick(self.tools,self.deliveries.append)
        self.assertEqual(self.deliveries,[])
        self.assertFalse(self.auto.change(id,"resume")["changed"])

    def test_watch_alerts_hysteresis_rearm_and_cooldown(self):
        job=self.auto.watch_gpu(75,1,1)
        id=job["id"]
        base=int(time.time())
        index=0
        def poll(value):
            nonlocal index
            # Use distinct, deterministic event times; the durable outbox
            # intentionally deduplicates same-job alerts within one second.
            event_time=base+index*10
            index+=1
            self._due(id,event_time-1)
            with patch("alfred.runtime.gpu_status",return_value={"gpu_temp_c":value}):
                self.auto.tick(self.tools,self.deliveries.append,now=event_time)
        poll(74)
        self.assertEqual(len(self.deliveries),0)
        poll(78)
        self.assertEqual(len(self.deliveries),1)
        poll(80)
        self.assertEqual(len(self.deliveries),1)
        poll(71) # below 75 - 3 C threshold, rearms
        poll(80) # cooldown prevents a second alert
        self.assertEqual(len(self.deliveries),1)
        with self.store.lock,self.store.db:
            self.store.db.execute(
                "UPDATE automation_jobs SET last_triggered_at=? WHERE id=?",
                (base-1000,id))
        poll(81)
        self.assertEqual(len(self.deliveries),2)
        self.assertIn("temperature alert",self.deliveries[-1]["message"])

    def test_watch_unavailable_metric_not_interpreted_as_zero(self):
        job=self.auto.watch_gpu(70,1,5)
        self._due(job["id"])
        with patch("alfred.runtime.gpu_status",return_value={
                 "available":False,"reason":"tegrastats unavailable"}):
            self.auto.tick(self.tools,self.deliveries.append)
        self.assertEqual(len(self.deliveries),0)
        self.assertEqual(self.auto.list_jobs()[0]["last_error"],"GPU temperature unavailable")

    def test_watch_input_boundaries(self):
        with self.assertRaises(ValueError):
            self.auto.watch_gpu(1000,1,1)
        with self.assertRaises(ValueError):
            self.auto.watch_gpu(75,0,1)
        with self.assertRaises(ValueError):
            self.auto.watch_gpu(75,1,0)

    def test_watch_read_only_and_no_shell(self):
        job=self.auto.watch_gpu(75,1,5)
        self._due(job["id"])
        with patch.object(self.tools,"execute",return_value={"gpu_temp_c":76}) as call:
            self.auto.tick(self.tools,self.deliveries.append)
        call.assert_called_once_with("get_gpu_status",{},from_plan=True)

    def test_tool_schemas_and_pause_require_approval(self):
        for name in ("create_reminder_at","create_daily_reminder","watch_gpu_temperature",
                     "list_automations","pause_automation","resume_automation",
                     "cancel_automation"):
            self.assertIn(name,SCHEMA_BY_NAME)
        sys=System2(Mock(),self.tools,self.store,{"max_plan_steps":5})
        result=sys.direct([{"name":"watch_gpu_temperature","arguments":{
            "threshold_c":80,"interval_minutes":5,"cooldown_minutes":30}}])
        self.assertIn("approval",result)
        self.assertEqual(self.auto.list_jobs(),[])
        sys.approve(result["approval"]["approval_id"])
        self.assertEqual(self.auto.list_jobs()[0]["kind"],"gpu_temperature_watch")
        uid=self.auto.list_jobs()[0]["id"]
        result=sys.direct([{"name":"pause_automation","arguments":{"automation_id":uid}}])
        self.assertIn("approval",result)
        sys.approve(result["approval"]["approval_id"])
        self.assertEqual(self.auto.list_jobs()[0]["status"],"paused")

    def test_relative_reminders_still_work(self):
        r=self.tools.execute("create_reminder",{"message":"legacy","delay_minutes":1})
        self.assertTrue(r["id"])
        self.assertEqual(len(self.store.list_reminders()),1)

    def test_no_mic_required_for_headless_scheduler_cli(self):
        from alfred.app import main
        with patch("alfred.app.AudioManager.ensure_input") as check,\
             patch("alfred.app.Alfred") as app:
            result=main(["--automations","--config","/does/not/exist.json"])
        self.assertEqual(result,0)
        check.assert_not_called()
        app.return_value.tools.automations.list_jobs.assert_called_once()


if __name__=="__main__":
    unittest.main()
