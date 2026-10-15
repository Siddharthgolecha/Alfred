"""Minimal durable operation journal for device-side meeting cleanup.

Opaque operation/meeting IDs only: never audio, transcript, PDF content,
paths, credentials or Qwen text. The trusted host must provide its existing
local SQLite Store; no dynamic provider or filesystem scanning is performed.
"""
from __future__ import annotations

from .cognitive.contracts import valid_id


class MeetingCleanupJournal:
    STATES=frozenset(("starting", "recording", "stopping",
                     "purge_pending", "rollback_pending"))

    def __init__(self, store):
        self.store=store
        with store.lock, store.db:
            store.db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_cleanup_operations (
                    operation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    meeting_id TEXT,
                    retained INTEGER NOT NULL,
                    state TEXT NOT NULL
                )
            """)

    def put(self, operation_id, session_id, task_id, owner, *,
            meeting_id=None, retained=False, state="starting"):
        valid_id(operation_id)
        valid_id(session_id)
        valid_id(task_id)
        if state not in self.STATES:
            raise ValueError("Invalid meeting cleanup state")
        with self.store.lock, self.store.db:
            self.store.db.execute("""
                INSERT INTO meeting_cleanup_operations
                (operation_id,session_id,task_id,owner,meeting_id,retained,state)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(operation_id) DO UPDATE SET
                meeting_id=excluded.meeting_id, state=excluded.state
            """, (operation_id,session_id,task_id,owner,meeting_id,
                   int(retained),state))

    def get(self, operation_id):
        valid_id(operation_id)
        with self.store.lock:
            row=self.store.db.execute(
                "SELECT * FROM meeting_cleanup_operations WHERE operation_id=?",
                (operation_id,)).fetchone()
        return dict(row) if row is not None else None

    def pending(self, session_id, owner):
        valid_id(session_id)
        with self.store.lock:
            rows=self.store.db.execute("""
                SELECT * FROM meeting_cleanup_operations
                WHERE session_id=? AND owner=? ORDER BY operation_id
            """,(session_id,owner)).fetchall()
        return [dict(r) for r in rows]

    def remove(self, operation_id):
        valid_id(operation_id)
        with self.store.lock,self.store.db:
            self.store.db.execute(
                "DELETE FROM meeting_cleanup_operations WHERE operation_id=?",
                (operation_id,))
