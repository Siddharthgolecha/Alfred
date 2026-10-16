"""Allowlisted system tools, durable reminders/schedules, and validated plans."""
from __future__ import annotations

from collections.abc import Callable
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time

from .constants import Limits, Timeouts, Truncation
from .platform_utils import sample_resources as _platform_sample_resources, system_uptime_seconds

LOG = logging.getLogger(__name__)

def schema(name: str, description: str, properties: dict | None = None,
           required: list | None = None) -> dict:
    """Return a JSON Schema tool definition object."""
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {},
                           "required": required or [], "additionalProperties": False}}


def _str_schema(description: str) -> dict:
    """Return a JSON Schema string property with the standard max length."""
    return {"type": "string", "description": description, "maxLength": Limits.TOOL_STR_MAX_LENGTH}


def _num_schema(description: str, lo: int, hi: int) -> dict:
    """Return a JSON Schema integer property with an explicit range."""
    return {"type": "integer", "description": description, "minimum": lo, "maximum": hi}


TOOL_SCHEMAS = [
    schema("get_system_status", "Report memory, CPU load, disk and uptime on Jetson"),
    schema("get_gpu_status", "Report Jetson GPU status, temperature and tegrastats sample"),
    schema("list_services", "List status of approved systemd user services"),
    schema("control_service", "Start or stop a preapproved user service",
           {"name":_str_schema("Exact allowed service name"), "action":{"type":"string","enum":["start","stop"]}}, ["name","action"]),
    schema("run_named_task", "Run a preconfigured safe script by task name",
           {"name":_str_schema("Exact approved named task")}, ["name"]),
    schema("create_reminder", "Create an alert in a number of minutes",
           {"message":_str_schema("Reminder text"),"delay_minutes":_num_schema("Delay in minutes",1,10080)}, ["message","delay_minutes"]),
    schema("create_reminder_at", "Remind at an explicit ISO-8601 date-time including timezone offset; example 2026-10-15T09:00:00+05:30",
           {"message":_str_schema("Reminder message"),"when":_str_schema("ISO-8601 date and clock time with +05:30 or Z")},["message","when"]),
    schema("create_daily_reminder", "Create a daily reminder at a local clock time in an explicit IANA timezone",
           {"message":_str_schema("Reminder message"),"local_time":_str_schema("HH:MM 24-hour clock"),
            "timezone":_str_schema("IANA timezone such as Asia/Kolkata")},["message","local_time","timezone"]),
    schema("watch_gpu_temperature", "Create recurring read-only GPU temperature alert; requires approval",
           {"threshold_c":_num_schema("Alert at or above Celsius threshold",20,110),
            "interval_minutes":_num_schema("Check frequency, minutes",1,1440),
            "cooldown_minutes":_num_schema("Minimum alert cooldown minutes",1,10080)},
           ["threshold_c","interval_minutes","cooldown_minutes"]),
    schema("list_automations","List active and paused absolute/daily reminders and GPU temperature watches"),
    schema("pause_automation","Pause a Phase 3 automation by numeric ID",
           {"automation_id":_num_schema("Automation ID",1,1000000000)},["automation_id"]),
    schema("resume_automation","Resume a paused Phase 3 automation by numeric ID",
           {"automation_id":_num_schema("Automation ID",1,1000000000)},["automation_id"]),
    schema("cancel_automation","Cancel a Phase 3 automation by numeric ID",
           {"automation_id":_num_schema("Automation ID",1,1000000000)},["automation_id"]),
    schema("list_reminders", "List scheduled pending reminders"),
    schema("cancel_reminder", "Cancel reminder by numeric identifier",
           {"reminder_id":_num_schema("Reminder id",1,1000000000)}, ["reminder_id"]),
    schema("schedule_every", "Run a readonly Jetson status tool repeatedly at a fixed interval",
           {"tool":{"type":"string","enum":["get_gpu_status","get_system_status"]},
            "interval_minutes":_num_schema("Interval in minutes",1,10080)}, ["tool","interval_minutes"]),
    schema("list_schedules", "List active recurring scheduled jobs"),
    schema("cancel_schedule", "Cancel recurring job by numeric identifier",
           {"schedule_id":_num_schema("Schedule id",1,1000000000)}, ["schedule_id"]),
    schema("run_workflow", "Execute a previously saved and validated named workflow",
           {"name":_str_schema("Existing workflow name")}, ["name"]),
    schema("list_workflows", "List saved named workflows"),
    schema("delegate_to_reasoner", "Use the Qwen System 2 planner to answer an explanation or multi-step request",
           {"request":_str_schema("Original spoken request or task")}, ["request"])
]
SCHEMA_BY_NAME = {s["name"]:s for s in TOOL_SCHEMAS}

def validate(name, args, allow_delegate=True):
    if name not in SCHEMA_BY_NAME:
        raise ValueError(f"Tool not allowlisted: {name}")
    if name == "delegate_to_reasoner" and not allow_delegate:
        raise ValueError("Nested reasoning is not allowed")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    params = SCHEMA_BY_NAME[name]["parameters"]
    if any(k not in params["properties"] for k in args):
        raise ValueError(f"Unexpected argument for {name}")
    if any(k not in args for k in params["required"]):
        raise ValueError(f"Missing argument for {name}")
    for k,v in args.items():
        d = params["properties"][k]
        if d["type"] == "integer" and (type(v) is not int or not d["minimum"] <= v <= d["maximum"]):
            raise ValueError(f"Invalid integer {k}")
        if d["type"] == "string" and (not isinstance(v,str) or len(v)>d.get("maxLength", Limits.TOOL_STR_MAX_LENGTH)
                                     or (d.get("enum") and v not in d["enum"])):
            raise ValueError(f"Invalid string {k}")
    return args

def utc_now():
    return int(time.time())

class Store:
    def __init__(self, file):
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

    def event(self, category: str, detail: dict) -> None:
        """Insert a single structured event row into the events table."""
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(at,category,detail) VALUES(?,?,?)",
                            (utc_now(),category,json.dumps(detail,default=str)[:6000]))

    def create_reminder(self, message: str, delay_minutes: int) -> dict:
        """Insert a pending reminder due in *delay_minutes* and return its record."""
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO reminders(message,due_at) VALUES(?,?)",
                                  (message,utc_now()+delay_minutes*60))
            return {"id":cur.lastrowid,"message":message,"due_at":utc_now()+delay_minutes*60}

    def list_reminders(self) -> list:
        """Return all pending reminders ordered by due time."""
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM reminders WHERE status='pending' ORDER BY due_at LIMIT 100")]

    def cancel_reminder(self, rid):
        with self.lock, self.db:
            cur = self.db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'",(rid,))
            return {"cancelled":cur.rowcount==1}

    def create_schedule(self, tool, minutes):
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO schedules(tool,interval_minutes,due_at) VALUES(?,?,?)",
                                  (tool,minutes,utc_now()+minutes*60))
            return {"id":cur.lastrowid,"tool":tool,"interval_minutes":minutes}

    def list_schedules(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM schedules WHERE active=1 ORDER BY id")]

    def cancel_schedule(self, sid):
        with self.lock, self.db:
            cur = self.db.execute("UPDATE schedules SET active=0 WHERE id=? AND active=1",(sid,))
            return {"cancelled":cur.rowcount==1}

    def save_workflow(self, name, steps):
        with self.lock, self.db:
            self.db.execute("INSERT INTO workflows(name,steps_json) VALUES(?,?) "
                            "ON CONFLICT(name) DO UPDATE SET steps_json=excluded.steps_json",
                            (name,json.dumps(steps)))
            return {"name":name,"steps":len(steps)}

    def list_workflows(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT name,steps_json FROM workflows ORDER BY name")]

    def get_workflow(self, name):
        with self.lock:
            r = self.db.execute("SELECT steps_json FROM workflows WHERE name=?",(name,)).fetchone()
            if r is None:
                raise ValueError(f"Unknown workflow: {name}")
            return json.loads(r["steps_json"])

    def due(self):
        """Claim due events transactionally so restarts do not replay a claimed alert."""
        now = utc_now()
        events = []
        with self.lock, self.db:
            for row in self.db.execute("SELECT * FROM reminders WHERE due_at<=? AND status='pending'",(now,)).fetchall():
                cur = self.db.execute("UPDATE reminders SET status='delivered' WHERE id=? AND status='pending'",(row["id"],))
                if cur.rowcount:
                    events.append(("reminder",dict(row)))
            for row in self.db.execute("SELECT * FROM schedules WHERE active=1 AND due_at<=?",(now,)).fetchall():
                # Drift-free catch-up: skip missed periods, do not flood after reboot.
                next_at = row["due_at"]
                delta = row["interval_minutes"] * 60
                while next_at <= now:
                    next_at += delta
                cur = self.db.execute("UPDATE schedules SET due_at=? WHERE id=? AND due_at=? AND active=1",
                                      (next_at,row["id"],row["due_at"]))
                if cur.rowcount:
                    events.append(("schedule",dict(row)))
        return events

def meminfo():
    """Return available and total memory in MiB; cross-platform."""
    sample = _platform_sample_resources()
    out = {}
    if sample.get("available_mib") is not None:
        out["MemAvailable"] = int(sample["available_mib"])
    return out

def gpu_status():
    """Take a bounded tegrastats sample without leaving a background process."""
    binary = shutil.which("tegrastats")
    if not binary:
        return {"available":False,"reason":"tegrastats not installed"}
    p = subprocess.Popen([binary,"--interval","1000"],stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    try:
        # select is available on all POSIX platforms (Linux, macOS); not on Windows.
        # tegrastats only exists on Jetson (Linux), so this code path is Linux-only.
        import select
        ready,_,_ = select.select([p.stdout], [], [], Timeouts.TEGRASTATS_READ)
        line = p.stdout.readline().strip() if ready else ""
    finally:
        p.terminate()
        try:
            p.wait(timeout=Timeouts.TEGRASTATS_TERMINATE)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=Timeouts.TEGRASTATS_TERMINATE)
        p.stdout.close()
    temps = {k:float(v) for k,v in re.findall(r"([A-Za-z_]+)@([0-9.]+)C",line)}
    return {"available":bool(line), "temperatures_c":temps,
            "gpu_temp_c":temps.get("GPU"),"raw":line[:Truncation.GPU_RAW]}

def status():
    disk = shutil.disk_usage("/")
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (None, None, None)
    return {"load_1_5_15": load,
            "memory_mib": meminfo(),
            "root_free_gib": round(disk.free / 1024**3, 2),
            "uptime_seconds": system_uptime_seconds()}

def _valid_name(name: str) -> bool:
    """Return True if *name* is a safe service or task name."""
    from .constants import Patterns
    return isinstance(name, str) and bool(Patterns.VALID_NAME.fullmatch(name))

def validate_plan(steps, max_steps=5, allow_cancellations=False):
    if not isinstance(steps,list) or len(steps)>max_steps:
        raise ValueError(f"Plan must contain <= {max_steps} steps")
    safe = []
    for ix,step in enumerate(steps):
        if not isinstance(step,dict) or set(step)-{"tool","args","when"}:
            raise ValueError("Invalid plan step keys")
        name = step.get("tool")
        validate(name,step.get("args",{}),allow_delegate=False)
        if name == "run_workflow" or (
            not allow_cancellations and name in ("cancel_reminder", "cancel_schedule")
        ):
            raise ValueError(f"{name} cannot appear in this plan")
        predicate = step.get("when")
        if predicate is not None:
            if not isinstance(predicate,dict) or set(predicate)!={"step","field","op","value"}:
                raise ValueError("Condition must have step,field,op,value")
            if type(predicate["step"]) is not int or not (0 <= predicate["step"]<ix):
                raise ValueError("Condition must refer to an earlier step")
            if predicate["op"] not in ("gt","gte","lt","lte","eq","neq"):
                raise ValueError("Unknown condition operator")
            if not isinstance(predicate["field"],str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",predicate["field"]):
                raise ValueError("Unsafe condition field")
            if type(predicate["value"]) not in (str,int,float,bool):
                raise ValueError("Invalid condition value")
        safe.append(step)
    return safe

def check_condition(predicate, results):
    actual = results[predicate["step"]].get(predicate["field"])
    expected = predicate["value"]
    if actual is None or type(actual) not in (str,int,float,bool):
        return False
    if predicate["op"] in ("gt","gte","lt","lte") and (
       type(actual) not in (int,float) or type(expected) not in (int,float)):
        return False
    op = predicate["op"]
    return {"eq":lambda a,b:a==b,"neq":lambda a,b:a!=b,
            "gt":lambda a,b:a>b,"gte":lambda a,b:a>=b,
            "lt":lambda a,b:a<b,"lte":lambda a,b:a<=b}[op](actual,expected)

class Tools:
    def __init__(self,cfg,store,processes=None):
        self.cfg = cfg
        self.store = store
        self.processes = processes
        self.services = set(cfg.get("allowed_services",[]))
        self.tasks = cfg.get("named_tasks",{})
        from .automations import Automations
        self.automations=Automations(store)
        from .workflow_engine import WorkflowEngine
        self.workflow_engine=WorkflowEngine(store,self)
        self._TOOL_HANDLERS: dict[str, Callable] = {
            "get_system_status":    self._handle_get_system_status,
            "get_gpu_status":       self._handle_get_gpu_status,
            "list_services":        self._handle_list_services,
            "control_service":      self._handle_control_service,
            "run_named_task":       self._handle_run_named_task,
            "create_reminder":      self._handle_create_reminder,
            "create_reminder_at":   self._handle_create_reminder_at,
            "create_daily_reminder":self._handle_create_daily_reminder,
            "watch_gpu_temperature":self._handle_watch_gpu_temperature,
            "list_automations":     self._handle_list_automations,
            "pause_automation":     self._handle_pause_automation,
            "resume_automation":    self._handle_resume_automation,
            "cancel_automation":    self._handle_cancel_automation,
            "list_reminders":       self._handle_list_reminders,
            "cancel_reminder":      self._handle_cancel_reminder,
            "schedule_every":       self._handle_schedule_every,
            "list_schedules":       self._handle_list_schedules,
            "cancel_schedule":      self._handle_cancel_schedule,
            "run_workflow":         self._handle_run_workflow,
            "list_workflows":       self._handle_list_workflows,
        }

    def _handle_get_system_status(self, args: dict) -> dict:
        return status()

    def _handle_get_gpu_status(self, args: dict) -> dict:
        return gpu_status()

    def _handle_list_services(self, args: dict) -> dict:
        return {service_name: self._service(service_name, "is-active") for service_name in self.services}

    def _handle_control_service(self, args: dict) -> dict:
        service_name = args["name"]
        action = args["action"]
        if service_name not in self.services or not _valid_name(service_name):
            raise PermissionError("Service not in runtime.allowed_services")
        return {"name": service_name, "action": action, "result": self._service(service_name, action)}

    def _handle_run_named_task(self, args: dict) -> dict:
        task_name = args["name"]
        spec = self.tasks.get(task_name)
        if not isinstance(spec, list) or not spec or any(not isinstance(arg, str) for arg in spec):
            raise PermissionError("Task not in runtime.named_tasks")
        runner = self.processes.run if self.processes else subprocess.run
        completed = runner(spec, capture_output=True, text=True, timeout=60, check=False)
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout[-Truncation.RESULT:],
            "stderr": completed.stderr[-Truncation.STDERR:],
        }

    def _handle_create_reminder(self, args: dict) -> dict:
        if not args["message"].strip():
            raise ValueError("Reminder cannot be blank")
        return self.store.create_reminder(args["message"], args["delay_minutes"])

    def _handle_create_reminder_at(self, args: dict) -> dict:
        return self.automations.remind_at(args["message"], args["when"])

    def _handle_create_daily_reminder(self, args: dict) -> dict:
        return self.automations.remind_daily(args["message"], args["local_time"], args["timezone"])

    def _handle_watch_gpu_temperature(self, args: dict) -> dict:
        return self.automations.watch_gpu(
            args["threshold_c"], args["interval_minutes"], args["cooldown_minutes"]
        )

    def _handle_list_automations(self, args: dict) -> list:
        return self.automations.list_jobs()

    def _handle_pause_automation(self, args: dict) -> dict:
        return self.automations.change(args["automation_id"], "pause")

    def _handle_resume_automation(self, args: dict) -> dict:
        return self.automations.change(args["automation_id"], "resume")

    def _handle_cancel_automation(self, args: dict) -> dict:
        return self.automations.change(args["automation_id"], "cancel")

    def _handle_list_reminders(self, args: dict) -> list:
        return self.store.list_reminders()

    def _handle_cancel_reminder(self, args: dict) -> dict:
        return self.store.cancel_reminder(args["reminder_id"])

    def _handle_schedule_every(self, args: dict) -> dict:
        return self.store.create_schedule(args["tool"], args["interval_minutes"])

    def _handle_list_schedules(self, args: dict) -> list:
        return self.store.list_schedules()

    def _handle_cancel_schedule(self, args: dict) -> dict:
        return self.store.cancel_schedule(args["schedule_id"])

    def _handle_run_workflow(self, args: dict) -> list:
        return self.run_plan(self.store.get_workflow(args["name"]))

    def _handle_list_workflows(self, args: dict) -> list:
        return [
            {"name": workflow["name"], "steps": len(json.loads(workflow["steps_json"]))}
            for workflow in self.store.list_workflows()
        ]

    def execute(self, name: str, args: dict, from_plan: bool = False) -> dict | list:
        validate(name, args, allow_delegate=not from_plan)
        if name == "delegate_to_reasoner":
            raise ValueError("Reasoner delegation is handled by the orchestrator")
        handler = self._TOOL_HANDLERS.get(name)
        if handler is None:
            raise PermissionError(f"Unknown tool: {name}")
        out = handler(args)
        self.store.event("tool", {"tool": name, "args": args, "result": out})
        return out

    def _service(self,n,verb):
        if n not in self.services or not _valid_name(n):
            raise PermissionError("Service not approved")
        runner = self.processes.run if self.processes else subprocess.run
        done = runner(["systemctl","--user",verb,n], capture_output=True,text=True,
                      timeout=10,check=False)
        return {"exit_code":done.returncode,"message":(done.stdout or done.stderr).strip()[:Truncation.MESSAGE]}

    def run_plan(self,steps, allow_cancellations=False):
        steps = validate_plan(steps, self.cfg.get("max_plan_steps",5),
                              allow_cancellations=allow_cancellations)
        results=[]
        for step in steps:
            if step.get("when") and not check_condition(step["when"],results):
                results.append({"skipped":True})
                continue
            result = self.execute(step["tool"],step.get("args",{}),from_plan=True)
            results.append(result if isinstance(result,dict) else {"items":result})
        return results

class Scheduler:
    def __init__(self,store,tools,on_reminder):
        self.store, self.tools, self.on_reminder = store,tools,on_reminder
        self.stop_flag=threading.Event()
        self.thread=threading.Thread(target=self._loop,name="alfred-scheduler",daemon=True)
        self.workflow_thread=threading.Thread(
            target=self._workflow_loop,name="alfred-workflow-worker",daemon=True)
        self.automations=tools.automations

    def start(self):
        self.thread.start()
        self.workflow_thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread.is_alive():
            self.thread.join(timeout=Timeouts.SCHEDULER_STOP)
        if self.workflow_thread.is_alive():
            # Active approved process calls have their own subprocess bounds.
            # Do not unsafely terminate an action or assume rollback.
            self.workflow_thread.join(timeout=Timeouts.SCHEDULER_STOP)

    def _loop(self):
        while not self.stop_flag.wait(2):
            for kind, event in self.store.due():
                try:
                    if kind == "reminder":
                        self.on_reminder(event)
                    else:
                        result=self.tools.execute(event["tool"],{},from_plan=True)
                        self.store.event("scheduled_result",{"id":event["id"],"result":result})
                except (OSError, ValueError, PermissionError) as exc:
                    LOG.exception("Scheduled event failed: %s", exc)
                    self.store.event("scheduler_error", {"event": event, "error": str(exc)})
            try:
                self.automations.tick(self.tools, self.on_reminder)
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.exception("Phase 3 scheduler iteration failed: %s", exc)
                self.store.event("automation_tick_error", {"error": str(exc)})


    def _workflow_loop(self):
        """Separate worker: 60-second named tasks cannot delay timer delivery."""
        while not self.stop_flag.wait(2):
            try:
                self.tools.workflow_engine.tick()
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.exception("Phase 4 workflow worker failed: %s", exc)
                self.store.event("workflow_tick_error", {"error": str(exc)})
