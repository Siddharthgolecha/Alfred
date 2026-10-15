"""Offline Phase 5 contracts, privacy, persistence and concurrency regressions."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from alfred.app import Alfred
from alfred.config import load_config
from alfred.cognitive import (TaskEnvelope, Decision, Observation, Escalation,
                              ApprovalReference, Completion, Diagnostic,
                              SessionLedger, MemorySessionLedger, decode_contract)
from alfred.runtime import Store


class ContractTests(unittest.TestCase):
    def test_strict_roundtrip_all_contracts(self):
        task = TaskEnvelope(session_id="a"*32, task_id="b"*32, source="text",
                            input_chars=12, started_at=123)
        examples = [
            task,
            Decision(engine="needle3", route="direct", call_count=2, confidence=0.9),
            Observation(tool="get_gpu_status", outcome="returned"),
            Escalation(source_model="needle3", target_model="qwen", reason="delegated"),
            ApprovalReference(approval_id=12),
            Completion(outcome="completed", reason="executed"),
            Diagnostic(name="needle_route", metrics={"latency_ms": 4, "input_chars": 12}),
        ]
        for item in examples:
            with self.subTest(kind=item.kind):
                self.assertEqual(decode_contract(item.to_json()), item)
                self.assertEqual(type(item).from_json(item.to_json()), item)

    def test_reject_unknown_fields_wrong_version_duplicate_keys_and_large_blob(self):
        valid = Decision(engine="needle3", route="direct", call_count=1).to_dict()
        for item in (
            {**valid, "version": 2},
            {**valid, "version": True},
            {**valid, "secret": "token"},
            {**valid, "confidence": float("nan")},
            {**valid, "call_count": True},
        ):
            with self.subTest(item=str(item)), self.assertRaises(ValueError):
                decode_contract(json.dumps(item))
        with self.assertRaises(ValueError):
            decode_contract('{"kind":"decision","kind":"decision","version":1}')
        with self.assertRaises(ValueError):
            decode_contract(" " * 4096)
        with self.assertRaises(ValueError):
            decode_contract('{"kind":"nope","version":1}')
        with self.assertRaises(ValueError):
            decode_contract("not json")

    def test_diagnostic_rejects_secrets_and_free_text(self):
        for payload in ({"api_key": "sk-test-secret"}, {"transcript":"hello"},
                        {"latency_ms": "5"}, {"input_chars": 100001}):
            with self.subTest(payload=payload), self.assertRaises((ValueError, Exception)):
                Diagnostic(name="needle_route", metrics=payload).to_json()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name)/"alfred.db"
        self.store = Store(self.database)
        self.ledger = SessionLedger(self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_order_restart_and_immutable_events(self):
        first = self.ledger.begin_task("text", 15)
        self.ledger.append(first.task_id, Decision(engine="needle3", route="direct", call_count=1))
        self.ledger.append(first.task_id, Observation(tool="get_system_status", outcome="returned"))
        self.ledger.append(first.task_id, Completion(outcome="completed", reason="executed"))
        with self.assertRaises(ValueError):
            self.ledger.append(first.task_id, Observation(tool="get_gpu_status", outcome="returned"))
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store.db:
                self.store.db.execute("UPDATE cognitive_events SET kind='fake' WHERE task_id=?",
                                      (first.task_id,))
        self.assertEqual([e.kind for e in self.ledger.events(first.task_id)],
                         ["task", "decision", "observation", "completion"])
        # A clean owner releases its claim; reconnecting is explicit recovery.
        self.ledger.release_ownership()
        self.store.db.close()
        self.store = Store(self.database)
        self.ledger = SessionLedger(self.store)
        self.ledger = SessionLedger(self.store)  # migrations idempotent
        self.assertEqual([e.kind for e in self.ledger.events(first.task_id)],
                         ["task", "decision", "observation", "completion"])
        self.assertEqual(self.ledger.resume_session(first.session_id), first.session_id)
        other = self.ledger.begin_task("voice", 8, first.session_id)
        self.assertNotEqual(first.task_id, other.task_id)
        self.assertEqual(len(self.ledger.list_tasks(first.session_id)), 2)

    def test_corrupted_event_rejected_before_append(self):
        task = self.ledger.begin_task("text", 5)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO cognitive_events(session_id,task_id,at,kind,contract_json) "
                "VALUES(?,?,?,?,?)", (task.session_id, task.task_id, 2, "decision", '{"invalid":1}'))
        with self.assertRaises(ValueError):
            self.ledger.events(task.task_id)
        with self.assertRaises(ValueError):
            self.ledger.append(task.task_id, Completion(outcome="completed", reason="no_action"))
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM cognitive_events WHERE task_id=?",
            (task.task_id,)).fetchone()[0], 2)

    def test_cross_session_row_or_wrong_task_metadata_fail_closed(self):
        task = self.ledger.begin_task("text", 3)
        other = self.ledger.begin_task("voice", 8)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO cognitive_events(session_id,task_id,at,kind,contract_json) "
                "VALUES(?,?,?,?,?)", (other.session_id, task.task_id, 2, "decision",
                                       Decision(engine="local", route="direct", call_count=0).to_json()))
        with self.assertRaisesRegex(ValueError, "Cross-session"):
            self.ledger.events(task.task_id)
        with self.assertRaisesRegex(ValueError, "Cross-session"):
            self.ledger.append(task.task_id, Completion(outcome="completed", reason="no_action"))
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM cognitive_events WHERE task_id=? AND kind='decision'",
                (task.task_id,))
            self.store.db.execute("UPDATE cognitive_tasks SET source='voice' WHERE id=?",
                                  (task.task_id,))
        with self.assertRaisesRegex(ValueError, "Corrupt task envelope"):
            self.ledger.events(task.task_id)

    def test_retention_does_not_purge_running_tasks_or_legacy_tables(self):
        self.store.event("legacy", {"note": "retained"})
        task = self.ledger.begin_task("text", 10)
        self.ledger.append(task.task_id, Completion(outcome="completed", reason="no_action"))
        running = self.ledger.begin_task("voice", 2)
        cutoff = int(time.time()) + 5
        # Even completed sessions cannot be pruned while their owner is alive.
        self.assertEqual(self.ledger.prune(cutoff), 0)
        self.ledger.release_ownership()
        self.assertEqual(self.ledger.prune(cutoff), 1)
        self.assertEqual(self.ledger.list_tasks(running.session_id)[0]["state"], "running")
        self.assertEqual(self.store.db.execute(
            "SELECT category FROM events").fetchone()[0], "legacy")
        with self.assertRaises(ValueError):
            self.ledger.events(task.task_id)

    def test_prune_does_not_delete_live_idle_session_between_commands(self):
        first=self.ledger.begin_task("voice", 8)
        self.ledger.append(first.task_id, Completion(outcome="completed", reason="no_action"))
        cutoff=int(time.time()) + 10
        # A separate retention client runs while the voice process remains
        # alive but has no running task. Its SQLite session claim must survive.
        other=Store(self.database)
        retention=SessionLedger(other)
        try:
            self.assertEqual(retention.prune(cutoff),0)
            self.assertEqual(len(self.ledger.list_tasks(first.session_id)),1)
            resumed=self.ledger.begin_task("voice", 12, first.session_id)
            self.assertEqual(resumed.session_id,first.session_id)
            self.ledger.append(resumed.task_id, Completion(outcome="completed", reason="no_action"))
            self.assertEqual(retention.prune(cutoff),0)
            self.ledger.release_ownership()
            self.assertEqual(retention.prune(cutoff),1)
            with self.assertRaises(ValueError):
                self.ledger.begin_task("voice",2,first.session_id)
        finally:
            other.db.close()

    def test_prune_dead_owner_after_completed_task(self):
        finished=self.ledger.begin_task("text",6)
        self.ledger.append(finished.task_id, Completion(outcome="completed", reason="no_action"))
        # Simulate a confirmed dead owner without an extra Linux process.
        with self.store.db:
            self.store.db.execute(
                "UPDATE cognitive_session_owners SET owner_pid=? WHERE session_id=?",
                (999999999,finished.session_id))
        self.assertEqual(self.ledger.prune(int(time.time())+10),1)
        self.assertEqual(self.ledger.list_tasks(finished.session_id),[])

    def test_concurrent_wal_clients_isolate_active_sessions(self):
        other = Store(self.database)
        ledger2 = SessionLedger(other)
        try:
            owned1 = self.ledger.begin_task("text", 1)
            owned2 = ledger2.begin_task("voice", 1)
            with self.assertRaisesRegex(RuntimeError, "active Alfred process"):
                ledger2.begin_task("voice", 2, owned1.session_id)
            self.assertEqual(self.ledger.list_tasks(owned1.session_id)[0]["state"],"running")
            ids, errors = [], []
            lock = threading.Lock()

            def produce(ledger, source, sid):
                try:
                    for _ in range(12):
                        task = ledger.begin_task(source, 5, sid)
                        ledger.append(task.task_id, Decision(engine="local", route="direct", call_count=0))
                        ledger.append(task.task_id, Completion(outcome="completed", reason="no_action"))
                        with lock:
                            ids.append(task.task_id)
                except Exception as exc:
                    with lock:
                        errors.append(exc)

            left = threading.Thread(target=produce, args=(self.ledger, "text",owned1.session_id))
            right = threading.Thread(target=produce, args=(ledger2, "voice",owned2.session_id))
            left.start(); right.start()
            left.join(10); right.join(10)
            self.assertFalse(left.is_alive() or right.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(ids), 24)
            self.assertEqual(len(set(ids)), 24)
            self.assertEqual(len(self.ledger.list_tasks(owned1.session_id)), 13)
            self.assertEqual(len(ledger2.list_tasks(owned2.session_id)), 13)
        finally:
            other.db.close()

    def test_memory_fake_matches_sqlite_interface(self):
        fake = MemorySessionLedger()
        try:
            task = fake.begin_task("reason", 4)
            fake.append(task.task_id, Completion(outcome="completed", reason="no_action"))
            self.assertEqual(len(fake.events(task.task_id)), 2)
        finally:
            fake.close()


class AlfredCorrelationTests(unittest.TestCase):
    def test_cli_command_single_task_no_secret_or_second_qwen(self):
        cfg = load_config()
        with tempfile.TemporaryDirectory() as home:
            cfg["runtime"]["database"] = str(Path(home)/"alfred.db")
            cfg["runtime"]["logs"] = home
            app = Alfred(cfg, load_models=False)
            app.cactus = Mock()
            app.cactus.route.return_value = {
                "confidence": 0.99,
                "function_calls": [{"name":"get_gpu_status", "arguments":{}}]}
            app.system2.direct = Mock(return_value={
                "message": "ok", "results": [{"api_key":"SECRET-TOOL-RESULT"}]})
            app.reason = Mock()
            app.feedback.tell = Mock()
            try:
                app.handle_text("Check GPU; password is SECRET-INPUT")
                tasks = app.sessions.list_tasks(app.session_id)
                self.assertEqual(len(tasks), 1)
                events = app.sessions.events(tasks[0]["id"])
                self.assertEqual([e.kind for e in events],
                                 ["task", "decision", "observation", "completion"])
                self.assertEqual(events[0].source, "text")
                self.assertEqual(events[-1].outcome, "completed")
                app.reason.assert_not_called()
                raw = "\n".join(r[0] for r in app.store.db.execute(
                    "SELECT contract_json FROM cognitive_events"))
                self.assertNotIn("SECRET", raw)
                self.assertNotIn("password", raw)
                self.assertNotIn("api_key", raw)
            finally:
                app.close()

    def test_voice_source_and_opt_in_metrics_exclude_transcript(self):
        cfg = load_config()
        cfg["runtime"]["cognitive_diagnostics"] = True
        with tempfile.TemporaryDirectory() as home:
            cfg["runtime"]["database"] = str(Path(home)/"alfred.db")
            cfg["runtime"]["logs"] = home
            app = Alfred(cfg, load_models=False)
            app.cactus = Mock()
            app.cactus.route.return_value = {"confidence":.95,
                "function_calls":[{"name":"get_system_status","arguments":{}}]}
            app.system2.direct = Mock(return_value={"message":"ok", "results":[{}]})
            app.feedback.tell = Mock()
            try:
                app._command_source = "voice"
                app.handle_text("secret=PRIVATE-TRANSCRIPT")
                task_id = app.sessions.list_tasks(app.session_id)[0]["id"]
                events = app.sessions.events(task_id)
                self.assertEqual(events[0].source, "voice")
                self.assertEqual(events[1].kind, "diagnostic")
                raw = "\n".join(e.to_json() for e in events)
                self.assertNotIn("PRIVATE", raw)
                self.assertNotIn("secret=", raw)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
