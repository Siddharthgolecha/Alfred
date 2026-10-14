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
        task = TaskEnvelope("a"*32, "b"*32, "text", 12, 123)
        examples = [
            task, Decision("needle3", "direct", 2, 0.9),
            Observation("get_gpu_status", "returned"),
            Escalation("needle3", "qwen", "delegated"),
            ApprovalReference(12), Completion("completed", "executed"),
            Diagnostic("needle_route", {"latency_ms": 4, "input_chars": 12}),
        ]
        for item in examples:
            with self.subTest(kind=item.kind):
                self.assertEqual(decode_contract(item.to_json()), item)
                self.assertEqual(type(item).from_json(item.to_json()), item)

    def test_reject_unknown_fields_wrong_version_duplicate_keys_and_large_blob(self):
        valid = Decision("needle3", "direct", 1).to_dict()
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
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                Diagnostic("needle_route", payload).to_json()


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
        self.ledger.append(first.task_id, Decision("needle3", "direct", 1))
        self.ledger.append(first.task_id, Observation("get_system_status", "returned"))
        self.ledger.append(first.task_id, Completion("completed", "executed"))
        with self.assertRaises(ValueError):
            self.ledger.append(first.task_id, Observation("get_gpu_status", "returned"))
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store.db:
                self.store.db.execute("UPDATE cognitive_events SET kind='fake' WHERE task_id=?",
                                      (first.task_id,))
        self.assertEqual([e.kind for e in self.ledger.events(first.task_id)],
                         ["task", "decision", "observation", "completion"])
        self.store.db.close()
        self.store = Store(self.database)
        self.ledger = SessionLedger(self.store)
        self.ledger = SessionLedger(self.store)  # migrations idempotent
        self.assertEqual([e.kind for e in self.ledger.events(first.task_id)],
                         ["task", "decision", "observation", "completion"])
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
            self.ledger.append(task.task_id, Completion("completed", "no_action"))
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM cognitive_events WHERE task_id=?",
            (task.task_id,)).fetchone()[0], 2)

    def test_retention_does_not_purge_running_tasks_or_legacy_tables(self):
        self.store.event("legacy", {"note": "retained"})
        task = self.ledger.begin_task("text", 10)
        self.ledger.append(task.task_id, Completion("completed", "no_action"))
        running = self.ledger.begin_task("voice", 2)
        cutoff = int(time.time()) + 5
        self.assertEqual(self.ledger.prune(cutoff), 1)
        self.assertEqual(self.ledger.list_tasks(running.session_id)[0]["state"], "running")
        self.assertEqual(self.store.db.execute(
            "SELECT category FROM events").fetchone()[0], "legacy")
        with self.assertRaises(ValueError):
            self.ledger.events(task.task_id)

    def test_concurrent_wal_clients_share_one_session(self):
        other = Store(self.database)
        ledger2 = SessionLedger(other)
        try:
            shared = self.ledger.begin_task("text", 1)
            ids, errors = [], []
            lock = threading.Lock()

            def produce(ledger, source):
                try:
                    for _ in range(12):
                        task = ledger.begin_task(source, 5, shared.session_id)
                        ledger.append(task.task_id, Decision("local", "direct", 0))
                        ledger.append(task.task_id, Completion("completed", "no_action"))
                        with lock:
                            ids.append(task.task_id)
                except Exception as exc:
                    with lock:
                        errors.append(exc)

            left = threading.Thread(target=produce, args=(self.ledger, "text"))
            right = threading.Thread(target=produce, args=(ledger2, "voice"))
            left.start(); right.start()
            left.join(10); right.join(10)
            self.assertFalse(left.is_alive() or right.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(ids), 24)
            self.assertEqual(len(set(ids)), 24)
            self.assertEqual(len(self.ledger.list_tasks(shared.session_id)), 25)
        finally:
            other.db.close()

    def test_memory_fake_matches_sqlite_interface(self):
        fake = MemorySessionLedger()
        try:
            task = fake.begin_task("reason", 4)
            fake.append(task.task_id, Completion("completed", "no_action"))
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
