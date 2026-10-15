"""Append-only SQLite WAL correlation ledger, independent of any model provider.

It extends Alfred's existing Store/SQLite connection. Old tables and event
history remain untouched. No user words or tool output are persisted.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from .contracts import (TaskEnvelope, Completion, Contract, decode_contract,
                        new_id, valid_id)

SCHEMA_VERSION = 1
MAX_TASK_EVENTS = 64


class SessionLedger:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db:
            store.db.execute("PRAGMA busy_timeout=5000")
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS cognitive_sessions(
                    id TEXT PRIMARY KEY, started_at INTEGER NOT NULL,
                    last_active_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cognitive_tasks(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES cognitive_sessions(id),
                    source TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'running');
                CREATE TABLE IF NOT EXISTS cognitive_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES cognitive_sessions(id),
                    task_id TEXT NOT NULL REFERENCES cognitive_tasks(id),
                    at INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    contract_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_cognitive_events_task
                    ON cognitive_events(task_id,id);
                CREATE INDEX IF NOT EXISTS idx_cognitive_tasks_session
                    ON cognitive_tasks(session_id,started_at);
                CREATE TRIGGER IF NOT EXISTS cognitive_events_immutable
                    BEFORE UPDATE ON cognitive_events
                    BEGIN SELECT RAISE(ABORT,'cognitive events are immutable'); END;
            """)
            version = store.db.execute("PRAGMA user_version").fetchone()[0]
            # We do not reuse user_version; legacy workflows may set it later.
            # Existence of namespaced tables is the idempotent v1 migration.
            if version < 0:
                raise ValueError("Invalid database user_version")

    def begin_task(self, source, input_chars, session_id=None):
        started = int(time.time())
        sid = valid_id(session_id) if session_id is not None else new_id()
        item = TaskEnvelope(sid, new_id(), source, input_chars, started)
        payload = item.to_json()
        with self.store.lock, self.store.db:
            row = self.store.db.execute(
                "SELECT id FROM cognitive_sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                if session_id is not None:
                    raise ValueError("Unknown session")
                self.store.db.execute(
                    "INSERT INTO cognitive_sessions(id,started_at,last_active_at) VALUES(?,?,?)",
                    (sid, started, started))
            self.store.db.execute(
                "INSERT INTO cognitive_tasks(id,session_id,source,started_at,state) "
                "VALUES(?,?,?,?,?)", (item.task_id, sid, source, started, "running"))
            self.store.db.execute(
                "INSERT INTO cognitive_events(session_id,task_id,at,kind,contract_json) "
                "VALUES(?,?,?,?,?)", (sid, item.task_id, started, item.kind, payload))
            self.store.db.execute(
                "UPDATE cognitive_sessions SET last_active_at=? WHERE id=?",
                (started, sid))
        return item

    def _history(self, task_id, session_id):
        task_row = self.store.db.execute(
            "SELECT source,started_at FROM cognitive_tasks WHERE id=? AND session_id=?",
            (task_id, session_id)).fetchone()
        if task_row is None:
            raise ValueError("Task/session integrity mismatch")
        rows = self.store.db.execute(
            "SELECT session_id,kind,contract_json FROM cognitive_events "
            "WHERE task_id=? ORDER BY id LIMIT ?",
            (task_id, MAX_TASK_EVENTS + 1)).fetchall()
        if not rows or len(rows) > MAX_TASK_EVENTS:
            raise ValueError("Missing, corrupt or oversized task history")
        history = []
        for row in rows:
            if row["session_id"] != session_id:
                raise ValueError("Cross-session event corruption")
            item = decode_contract(row["contract_json"])
            if item.kind != row["kind"]:
                raise ValueError("Corrupt event kind")
            history.append(item)
        first = history[0]
        if (not isinstance(first, TaskEnvelope) or first.task_id != task_id
                or first.session_id != session_id or first.source != task_row["source"]
                or first.started_at != task_row["started_at"]):
            raise ValueError("Corrupt task envelope")
        if any(isinstance(x, TaskEnvelope) for x in history[1:]):
            raise ValueError("Duplicate task envelope")
        if any(isinstance(x, Completion) for x in history[:-1]):
            raise ValueError("Events found after completion")
        return history

    def append(self, task_id, event):
        valid_id(task_id)
        if not isinstance(event, Contract) or isinstance(event, TaskEnvelope):
            raise ValueError("Expected a non-task cognitive event")
        payload = event.to_json()
        now = int(time.time())
        with self.store.lock, self.store.db:
            task = self.store.db.execute(
                "SELECT id,session_id,state FROM cognitive_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("Unknown task")
            if task["state"] != "running":
                raise ValueError("Cannot append to closed task")
            history = self._history(task_id, task["session_id"])
            if len(history) >= MAX_TASK_EVENTS or isinstance(history[-1], Completion):
                raise ValueError("Task already completed or event limit reached")
            self.store.db.execute(
                "INSERT INTO cognitive_events(session_id,task_id,at,kind,contract_json) "
                "VALUES(?,?,?,?,?)",
                (task["session_id"], task_id, now, event.kind, payload))
            if isinstance(event, Completion):
                self.store.db.execute(
                    "UPDATE cognitive_tasks SET state=? WHERE id=? AND state='running'",
                    (event.outcome, task_id))
            self.store.db.execute(
                "UPDATE cognitive_sessions SET last_active_at=? WHERE id=?",
                (now, task["session_id"]))
        return event

    def events(self, task_id):
        valid_id(task_id)
        with self.store.lock:
            task = self.store.db.execute(
                "SELECT session_id,state FROM cognitive_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("Unknown task")
            history = self._history(task_id, task["session_id"])
            final = history[-1]
            if ((task["state"] == "running" and isinstance(final, Completion))
                    or (task["state"] != "running" and
                        (not isinstance(final, Completion) or task["state"] != final.outcome))):
                raise ValueError("Corrupt task state")
            return history

    def list_tasks(self, session_id, limit=100):
        valid_id(session_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid list limit")
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute(
                "SELECT id,session_id,source,started_at,state FROM cognitive_tasks "
                "WHERE session_id=? ORDER BY started_at,id LIMIT ?",
                (session_id, limit))]

    def prune(self, before_epoch):
        """Explicit retention; only finished, inactive sessions can be pruned."""
        if type(before_epoch) is not int or before_epoch < 0:
            raise ValueError("Invalid cutoff")
        with self.store.lock, self.store.db:
            rows = self.store.db.execute(
                "SELECT id FROM cognitive_sessions WHERE last_active_at<? "
                "AND NOT EXISTS (SELECT 1 FROM cognitive_tasks t "
                "WHERE t.session_id=cognitive_sessions.id AND t.state='running')",
                (before_epoch,)).fetchall()
            for row in rows:
                sid = row["id"]
                self.store.db.execute("DELETE FROM cognitive_events WHERE session_id=?", (sid,))
                self.store.db.execute("DELETE FROM cognitive_tasks WHERE session_id=?", (sid,))
                self.store.db.execute("DELETE FROM cognitive_sessions WHERE id=?", (sid,))
            return len(rows)


class MemorySessionLedger:
    """SQLite in-memory fake using the identical migration, codec and methods."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=MEMORY")
        self.ledger = SessionLedger(self)

    def __getattr__(self, name):
        return getattr(self.ledger, name)

    def close(self):
        self.db.close()
