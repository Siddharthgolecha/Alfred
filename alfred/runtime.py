"""Allowlisted system tools, durable reminders/schedules, and validated plans."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .constants import Limits, Timeouts, Truncation
from .platform_utils import sample_resources as _platform_sample_resources, system_uptime_seconds
from .tools import TOOL_SCHEMAS, SCHEMA_BY_NAME, validate, default_registry

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thin schema helpers kept for internal callers that still reference them
# ---------------------------------------------------------------------------

def schema(name: str, description: str, properties: dict | None = None,
           required: list | None = None) -> dict:
    """Return a JSON Schema tool definition object."""
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {},
                           "required": required or [], "additionalProperties": False}}


def _str_schema(description: str) -> dict:
    return {"type": "string", "description": description, "maxLength": Limits.TOOL_STR_MAX_LENGTH}


def _num_schema(description: str, lo: int, hi: int) -> dict:
    return {"type": "integer", "description": description, "minimum": lo, "maximum": hi}


def utc_now() -> int:
    return int(time.time())


def validate_plan(
    steps: list[dict[str, Any]],
    max_steps: int = 5,
    allow_cancellations: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or len(steps) > max_steps:
        raise ValueError(f"Plan must contain <= {max_steps} steps")
    safe = []
    for ix, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) - {"tool", "args", "when"}:
            raise ValueError("Invalid plan step keys")
        name = step.get("tool")
        validate(name, step.get("args", {}), allow_delegate=False)
        if name == "run_workflow" or (
            not allow_cancellations and name in ("cancel_reminder", "cancel_schedule")
        ):
            raise ValueError(f"{name} cannot appear in this plan")
        predicate = step.get("when")
        if predicate is not None:
            if not isinstance(predicate, dict) or set(predicate) != {"step", "field", "op", "value"}:
                raise ValueError("Condition must have step,field,op,value")
            if type(predicate["step"]) is not int or not (0 <= predicate["step"] < ix):
                raise ValueError("Condition must refer to an earlier step")
            if predicate["op"] not in ("gt", "gte", "lt", "lte", "eq", "neq"):
                raise ValueError("Unknown condition operator")
            if not isinstance(predicate["field"], str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", predicate["field"]
            ):
                raise ValueError("Unsafe condition field")
            if type(predicate["value"]) not in (str, int, float, bool):
                raise ValueError("Invalid condition value")
        safe.append(step)
    return safe


def check_condition(predicate: dict[str, Any], results: list[dict[str, Any]]) -> bool:
    actual = results[predicate["step"]].get(predicate["field"])
    expected = predicate["value"]
    if actual is None or type(actual) not in (str, int, float, bool):
        return False
    if predicate["op"] in ("gt", "gte", "lt", "lte") and (
        type(actual) not in (int, float) or type(expected) not in (int, float)
    ):
        return False
    op = predicate["op"]
    return {
        "eq": lambda a, b: a == b, "neq": lambda a, b: a != b,
        "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
        "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
    }[op](actual, expected)


# ---------------------------------------------------------------------------
# Store domain mixins
# ---------------------------------------------------------------------------

class _ReminderStore:
    """SQLite-backed reminders domain."""

    def create_reminder(self, message: str, delay_minutes: int) -> dict[str, Any]:
        """Insert a pending reminder due in *delay_minutes* and return its record."""
        with self.lock, self.db:  # type: ignore[attr-defined]
            cur = self.db.execute(
                "INSERT INTO reminders(message,due_at) VALUES(?,?)",
                (message, utc_now() + delay_minutes * 60),
            )
            return {"id": cur.lastrowid, "message": message,
                    "due_at": utc_now() + delay_minutes * 60}

    def list_reminders(self) -> list[dict[str, Any]]:
        """Return all pending reminders ordered by due time."""
        with self.lock:  # type: ignore[attr-defined]
            return [dict(r) for r in self.db.execute(  # type: ignore[attr-defined]
                "SELECT * FROM reminders WHERE status='pending' ORDER BY due_at LIMIT 100"
            )]

    def cancel_reminder(self, rid: int) -> dict[str, Any]:
        with self.lock, self.db:  # type: ignore[attr-defined]
            cur = self.db.execute(
                "UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'", (rid,)
            )
            return {"cancelled": cur.rowcount == 1}


class _ScheduleStore:
    """SQLite-backed schedules domain."""

    def create_schedule(self, tool: str, minutes: int) -> dict[str, Any]:
        with self.lock, self.db:  # type: ignore[attr-defined]
            cur = self.db.execute(
                "INSERT INTO schedules(tool,interval_minutes,due_at) VALUES(?,?,?)",
                (tool, minutes, utc_now() + minutes * 60),
            )
            return {"id": cur.lastrowid, "tool": tool, "interval_minutes": minutes}

    def list_schedules(self) -> list[dict[str, Any]]:
        with self.lock:  # type: ignore[attr-defined]
            return [dict(r) for r in self.db.execute(  # type: ignore[attr-defined]
                "SELECT * FROM schedules WHERE active=1 ORDER BY id"
            )]

    def cancel_schedule(self, sid: int) -> dict[str, Any]:
        with self.lock, self.db:  # type: ignore[attr-defined]
            cur = self.db.execute(
                "UPDATE schedules SET active=0 WHERE id=? AND active=1", (sid,)
            )
            return {"cancelled": cur.rowcount == 1}

    def due_schedules(self, now: int) -> list:
        """Return all active schedules whose due_at has passed."""
        return self.db.execute(  # type: ignore[attr-defined]
            "SELECT * FROM schedules WHERE active=1 AND due_at<=?", (now,)
        ).fetchall()

    def advance_schedule(self, row_id: int, old_due_at: int, new_due_at: int) -> int:
        """Drift-free reschedule; returns rowcount (1 on success, 0 on conflict)."""
        cur = self.db.execute(  # type: ignore[attr-defined]
            "UPDATE schedules SET due_at=? WHERE id=? AND due_at=? AND active=1",
            (new_due_at, row_id, old_due_at),
        )
        return cur.rowcount


class _WorkflowStore:
    """SQLite-backed workflows domain."""

    def save_workflow(self, name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock, self.db:  # type: ignore[attr-defined]
            self.db.execute(
                "INSERT INTO workflows(name,steps_json) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET steps_json=excluded.steps_json",
                (name, json.dumps(steps)),
            )
            return {"name": name, "steps": len(steps)}

    def list_workflows(self) -> list[dict[str, Any]]:
        with self.lock:  # type: ignore[attr-defined]
            return [dict(r) for r in self.db.execute(  # type: ignore[attr-defined]
                "SELECT name,steps_json FROM workflows ORDER BY name"
            )]

    def get_workflow(self, name: str) -> list[dict[str, Any]]:
        with self.lock:  # type: ignore[attr-defined]
            r = self.db.execute(  # type: ignore[attr-defined]
                "SELECT steps_json FROM workflows WHERE name=?", (name,)
            ).fetchone()
            if r is None:
                raise ValueError(f"Unknown workflow: {name}")
            return json.loads(r["steps_json"])


class _EventStore:
    """SQLite-backed structured event log."""

    def event(self, category: str, detail: dict) -> None:
        """Insert a single structured event row into the events table."""
        with self.lock, self.db:  # type: ignore[attr-defined]
            self.db.execute(
                "INSERT INTO events(at,category,detail) VALUES(?,?,?)",
                (utc_now(), category, json.dumps(detail, default=str)[:6000]),
            )


# ---------------------------------------------------------------------------
# Store — composes all domain mixins
# ---------------------------------------------------------------------------

class Store(_ReminderStore, _ScheduleStore, _WorkflowStore, _EventStore):
    """Durable SQLite store composed of domain-specific query mixins."""

    def __init__(self, file: str) -> None:
        path = Path(file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reminders(
                id INTEGER PRIMARY KEY, message TEXT NOT NULL,
                due_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS schedules(
                id INTEGER PRIMARY KEY, tool TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
                due_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS workflows(
                name TEXT PRIMARY KEY, steps_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, at INTEGER NOT NULL, category TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            self.db.commit()

    def due(self) -> list[tuple[str, dict[str, Any]]]:
        """Claim due events transactionally so restarts do not replay a claimed alert."""
        now = utc_now()
        events = []
        with self.lock, self.db:
            for row in self.db.execute(
                "SELECT * FROM reminders WHERE due_at<=? AND status='pending'", (now,)
            ).fetchall():
                cur = self.db.execute(
                    "UPDATE reminders SET status='delivered' WHERE id=? AND status='pending'",
                    (row["id"],),
                )
                if cur.rowcount:
                    events.append(("reminder", dict(row)))
            for row in self.due_schedules(now):
                next_at = row["due_at"]
                delta = row["interval_minutes"] * 60
                while next_at <= now:
                    next_at += delta
                if self.advance_schedule(row["id"], row["due_at"], next_at):
                    events.append(("schedule", dict(row)))
        return events


# ---------------------------------------------------------------------------
# System helpers (kept in runtime for callers that import directly)
# ---------------------------------------------------------------------------

def meminfo() -> dict[str, int]:
    """Return available and total memory in MiB; cross-platform."""
    sample = _platform_sample_resources()
    out: dict[str, int] = {}
    if sample.get("available_mib") is not None:
        out["MemAvailable"] = int(sample["available_mib"])
    return out


def gpu_status() -> dict[str, Any]:
    """Take a bounded tegrastats sample without leaving a background process."""
    import shutil
    binary = shutil.which("tegrastats")
    if not binary:
        return {"available": False, "reason": "tegrastats not installed"}
    p = subprocess.Popen(
        [binary, "--interval", "1000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        import select
        ready, _, _ = select.select([p.stdout], [], [], Timeouts.TEGRASTATS_READ)
        line = p.stdout.readline().strip() if ready else ""
    finally:
        p.terminate()
        try:
            p.wait(timeout=Timeouts.TEGRASTATS_TERMINATE)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=Timeouts.TEGRASTATS_TERMINATE)
        p.stdout.close()
    temps = {k: float(v) for k, v in re.findall(r"([A-Za-z_]+)@([0-9.]+)C", line)}
    return {
        "available": bool(line),
        "temperatures_c": temps,
        "gpu_temp_c": temps.get("GPU"),
        "raw": line[:Truncation.GPU_RAW],
    }


def status() -> dict[str, Any]:
    import os
    import shutil
    disk = shutil.disk_usage("/")
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (None, None, None)
    return {
        "load_1_5_15": load,
        "memory_mib": meminfo(),
        "root_free_gib": round(disk.free / 1024**3, 2),
        "uptime_seconds": system_uptime_seconds(),
    }


def _valid_name(name: str) -> bool:
    """Return True if *name* is a safe service or task name."""
    from .constants import Patterns
    return isinstance(name, str) and bool(Patterns.VALID_NAME.fullmatch(name))


# ---------------------------------------------------------------------------
# Tools — delegates execution to default_registry
# ---------------------------------------------------------------------------

class Tools:
    def __init__(
        self,
        cfg: dict[str, Any],
        store: "Store",
        processes: Any = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.processes = processes
        self.services: set[str] = set(cfg.get("allowed_services", []))
        self.tasks: dict[str, Any] = cfg.get("named_tasks", {})
        from .automations import Automations
        self.automations = Automations(store)
        from .workflow_engine import WorkflowEngine
        self.workflow_engine = WorkflowEngine(store, self)

    # ------------------------------------------------------------------
    # Private handler methods — kept for direct test access
    # ------------------------------------------------------------------

    def _handle_get_system_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return status()

    def _handle_get_gpu_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return gpu_status()

    def _handle_list_services(self, args: dict[str, Any]) -> dict[str, Any]:
        return {svc: self._service(svc, "is-active") for svc in self.services}

    def _handle_control_service(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        action = args["action"]
        if name not in self.services or not _valid_name(name):
            raise PermissionError("Service not in runtime.allowed_services")
        return {"name": name, "action": action, "result": self._service(name, action)}

    def _handle_run_named_task(self, args: dict[str, Any]) -> dict[str, Any]:
        task_name = args["name"]
        spec = self.tasks.get(task_name)
        if not isinstance(spec, list) or not spec or any(not isinstance(a, str) for a in spec):
            raise PermissionError("Task not in runtime.named_tasks")
        runner = self.processes.run if self.processes else subprocess.run
        completed = runner(spec, capture_output=True, text=True, timeout=60, check=False)
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout[-Truncation.RESULT:],
            "stderr": completed.stderr[-Truncation.STDERR:],
        }

    def _handle_create_reminder(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args["message"].strip():
            raise ValueError("Reminder cannot be blank")
        return self.store.create_reminder(args["message"], args["delay_minutes"])

    def _handle_create_reminder_at(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.automations.remind_at(args["message"], args["when"])

    def _handle_create_daily_reminder(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.automations.remind_daily(args["message"], args["local_time"], args["timezone"])

    def _handle_watch_gpu_temperature(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.automations.watch_gpu(
            args["threshold_c"], args["interval_minutes"], args["cooldown_minutes"]
        )

    def _handle_list_automations(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self.automations.list_jobs()

    def _handle_pause_automation(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.automations.change(args["automation_id"], "pause")

    def _handle_resume_automation(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.automations.change(args["automation_id"], "resume")

    def _handle_cancel_automation(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.automations.change(args["automation_id"], "cancel")

    def _handle_list_reminders(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.list_reminders()

    def _handle_cancel_reminder(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.store.cancel_reminder(args["reminder_id"])

    def _handle_schedule_every(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.store.create_schedule(args["tool"], args["interval_minutes"])

    def _handle_list_schedules(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.list_schedules()

    def _handle_cancel_schedule(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.store.cancel_schedule(args["schedule_id"])

    def _handle_run_workflow(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self.run_plan(self.store.get_workflow(args["name"]))

    def _handle_list_workflows(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"name": wf["name"], "steps": len(json.loads(wf["steps_json"]))}
            for wf in self.store.list_workflows()
        ]

    # ------------------------------------------------------------------
    # Public execution interface — dispatches via default_registry
    # ------------------------------------------------------------------

    def execute(self, name: str, args: dict[str, Any], from_plan: bool = False) -> dict[str, Any] | list[Any]:
        validate(name, args, allow_delegate=not from_plan)
        if name == "delegate_to_reasoner":
            raise ValueError("Reasoner delegation is handled by the orchestrator")
        out = default_registry.execute(name, args, self)
        self.store.event("tool", {"tool": name, "args": args, "result": out})
        return out

    def _service(self, n: str, verb: str) -> dict[str, Any]:
        if n not in self.services or not _valid_name(n):
            raise PermissionError("Service not approved")
        runner = self.processes.run if self.processes else subprocess.run
        done = runner(
            ["systemctl", "--user", verb, n],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return {"exit_code": done.returncode,
                "message": (done.stdout or done.stderr).strip()[:Truncation.MESSAGE]}

    def run_plan(
        self,
        steps: list[dict[str, Any]],
        allow_cancellations: bool = False,
    ) -> list[dict[str, Any] | list[Any]]:
        steps = validate_plan(steps, self.cfg.get("max_plan_steps", 5),
                              allow_cancellations=allow_cancellations)
        results = []
        for step in steps:
            if step.get("when") and not check_condition(step["when"], results):
                results.append({"skipped": True})
                continue
            result = self.execute(step["tool"], step.get("args", {}), from_plan=True)
            results.append(result if isinstance(result, dict) else {"items": result})
        return results


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(
        self,
        store: Store,
        tools: Tools,
        on_reminder: Any,
    ) -> None:
        self.store, self.tools, self.on_reminder = store, tools, on_reminder
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(
            target=self._loop, name="alfred-scheduler", daemon=True
        )
        self.workflow_thread = threading.Thread(
            target=self._workflow_loop, name="alfred-workflow-worker", daemon=True
        )
        self.automations = tools.automations

    def start(self) -> None:
        self.thread.start()
        self.workflow_thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        if self.thread.is_alive():
            self.thread.join(timeout=Timeouts.SCHEDULER_STOP)
        if self.workflow_thread.is_alive():
            self.workflow_thread.join(timeout=Timeouts.SCHEDULER_STOP)

    def _loop(self) -> None:
        while not self.stop_flag.wait(2):
            for kind, event in self.store.due():
                try:
                    if kind == "reminder":
                        self.on_reminder(event)
                    else:
                        result = self.tools.execute(event["tool"], {}, from_plan=True)
                        self.store.event("scheduled_result", {"id": event["id"], "result": result})
                except (OSError, ValueError, PermissionError) as exc:
                    LOG.exception("Scheduled event failed: %s", exc)
                    self.store.event("scheduler_error", {"event": event, "error": str(exc)})
            try:
                self.automations.tick(self.tools, self.on_reminder)
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.exception("Phase 3 scheduler iteration failed: %s", exc)
                self.store.event("automation_tick_error", {"error": str(exc)})

    def _workflow_loop(self) -> None:
        """Separate worker: 60-second named tasks cannot delay timer delivery."""
        while not self.stop_flag.wait(2):
            try:
                self.tools.workflow_engine.tick()
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.exception("Phase 4 workflow worker failed: %s", exc)
                self.store.event("workflow_tick_error", {"error": str(exc)})
