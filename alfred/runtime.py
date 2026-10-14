"""Allowlisted system tools, durable reminders/schedules, and validated plans."""
from __future__ import annotations

from datetime import datetime, timezone
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

LOG = logging.getLogger(__name__)

def schema(name, description, properties=None, required=None):
    return {"name":name, "description":description,
            "parameters":{"type":"object", "properties":properties or {},
                          "required":required or [], "additionalProperties":False}}

STR = lambda description: {"type":"string", "description":description, "maxLength":240}
NUM = lambda description,lo,hi: {"type":"integer","description":description,"minimum":lo,"maximum":hi}
TOOL_SCHEMAS = [
    schema("get_system_status", "Report memory, CPU load, disk and uptime on Jetson"),
    schema("get_gpu_status", "Report Jetson GPU status, temperature and tegrastats sample"),
    schema("list_services", "List status of approved systemd user services"),
    schema("control_service", "Start or stop a preapproved user service",
           {"name":STR("Exact allowed service name"), "action":{"type":"string","enum":["start","stop"]}}, ["name","action"]),
    schema("run_named_task", "Run a preconfigured safe script by task name",
           {"name":STR("Exact approved named task")}, ["name"]),
    schema("create_reminder", "Create an alert in a number of minutes",
           {"message":STR("Reminder text"),"delay_minutes":NUM("Delay in minutes",1,10080)}, ["message","delay_minutes"]),
    schema("list_reminders", "List scheduled pending reminders"),
    schema("cancel_reminder", "Cancel reminder by numeric identifier",
           {"reminder_id":NUM("Reminder id",1,1000000000)}, ["reminder_id"]),
    schema("schedule_every", "Run a readonly Jetson status tool repeatedly at a fixed interval",
           {"tool":{"type":"string","enum":["get_gpu_status","get_system_status"]},
            "interval_minutes":NUM("Interval in minutes",1,10080)}, ["tool","interval_minutes"]),
    schema("list_schedules", "List active recurring scheduled jobs"),
    schema("cancel_schedule", "Cancel recurring job by numeric identifier",
           {"schedule_id":NUM("Schedule id",1,1000000000)}, ["schedule_id"]),
    schema("run_workflow", "Execute a previously saved and validated named workflow",
           {"name":STR("Existing workflow name")}, ["name"]),
    schema("list_workflows", "List saved named workflows"),
    schema("delegate_to_reasoner", "Use the Qwen System 2 planner to answer an explanation or multi-step request",
           {"request":STR("Original spoken request or task")}, ["request"])
]
SCHEMA_BY_NAME = {s["name"]:s for s in TOOL_SCHEMAS}

def validate(name, args, allow_delegate=True):
    if name not in SCHEMA_BY_NAME:
        raise ValueError(f"Tool not allowlisted: {name}")
    if name == "delegate_to_reasoner" and not allow_delegate:
        raise ValueError("Nested reasoning is not allowed")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    p = SCHEMA_BY_NAME[name]["parameters"]
    if any(k not in p["properties"] for k in args):
        raise ValueError(f"Unexpected argument for {name}")
    if any(k not in args for k in p["required"]):
        raise ValueError(f"Missing argument for {name}")
    for k,v in args.items():
        d = p["properties"][k]
        if d["type"] == "integer" and (type(v) is not int or not d["minimum"] <= v <= d["maximum"]):
            raise ValueError(f"Invalid integer {k}")
        if d["type"] == "string" and (not isinstance(v,str) or len(v)>d.get("maxLength",240)
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

    def event(self, category, detail):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(at,category,detail) VALUES(?,?,?)",
                            (utc_now(),category,json.dumps(detail,default=str)[:6000]))

    def create_reminder(self, message, delay_minutes):
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO reminders(message,due_at) VALUES(?,?)",
                                  (message,utc_now()+delay_minutes*60))
            return {"id":cur.lastrowid,"message":message,"due_at":utc_now()+delay_minutes*60}

    def list_reminders(self):
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
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(("MemTotal:", "MemAvailable:")):
            name, value = line.split(":",1)
            out[name] = int(value.strip().split()[0]) // 1024
    return out

def gpu_status():
    """Take a bounded tegrastats sample without leaving a background process."""
    binary = shutil.which("tegrastats")
    if not binary:
        return {"available":False,"reason":"tegrastats not installed"}
    p = subprocess.Popen([binary,"--interval","1000"],stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    try:
        import select
        ready,_,_ = select.select([p.stdout], [], [], 4)
        line = p.stdout.readline().strip() if ready else ""
    finally:
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
        p.stdout.close()
    temps = {k:float(v) for k,v in re.findall(r"([A-Za-z_]+)@([0-9.]+)C",line)}
    return {"available":bool(line), "temperatures_c":temps,
            "gpu_temp_c":temps.get("GPU"),"raw":line[:700]}

def status():
    disk = shutil.disk_usage("/")
    return {"load_1_5_15":os.getloadavg(),
            "memory_mib":meminfo(),
            "root_free_gib":round(disk.free/1024**3,2),
            "uptime_seconds":round(float(Path("/proc/uptime").read_text().split()[0]))}

def _valid_name(s):
    return isinstance(s,str) and bool(re.fullmatch(r"[a-zA-Z0-9_.@-]{1,80}",s))

def validate_plan(steps, max_steps=5):
    if not isinstance(steps,list) or len(steps)>max_steps:
        raise ValueError(f"Plan must contain <= {max_steps} steps")
    safe = []
    for ix,step in enumerate(steps):
        if not isinstance(step,dict) or set(step)-{"tool","args","when"}:
            raise ValueError("Invalid plan step keys")
        name = step.get("tool")
        validate(name,step.get("args",{}),allow_delegate=False)
        if name in ("run_workflow", "cancel_reminder", "cancel_schedule"):
            raise ValueError(f"{name} cannot appear in generated plans")
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
    def __init__(self,cfg,store):
        self.cfg = cfg
        self.store = store
        self.services = set(cfg.get("allowed_services",[]))
        self.tasks = cfg.get("named_tasks",{})

    def execute(self,name,args,from_plan=False):
        validate(name,args,allow_delegate=not from_plan)
        if name == "delegate_to_reasoner":
            raise ValueError("Reasoner delegation is handled by the orchestrator")
        if name == "get_system_status":
            out = status()
        elif name == "get_gpu_status":
            out = gpu_status()
        elif name == "list_services":
            out = {n:self._service(n,"is-active") for n in self.services}
        elif name == "control_service":
            n, action = args["name"],args["action"]
            if n not in self.services or not _valid_name(n):
                raise PermissionError("Service not in runtime.allowed_services")
            out = {"name":n,"action":action,"result":self._service(n,action)}
        elif name == "run_named_task":
            n = args["name"]
            spec = self.tasks.get(n)
            if not isinstance(spec,list) or not spec or any(not isinstance(a,str) for a in spec):
                raise PermissionError("Task not in runtime.named_tasks")
            done = subprocess.run(spec,capture_output=True,text=True,timeout=60,check=False)
            out = {"returncode":done.returncode,"stdout":done.stdout[-1200:],"stderr":done.stderr[-600:]}
        elif name == "create_reminder":
            if not args["message"].strip():
                raise ValueError("Reminder cannot be blank")
            out = self.store.create_reminder(args["message"],args["delay_minutes"])
        elif name == "list_reminders":
            out = self.store.list_reminders()
        elif name == "cancel_reminder":
            out = self.store.cancel_reminder(args["reminder_id"])
        elif name == "schedule_every":
            out = self.store.create_schedule(args["tool"],args["interval_minutes"])
        elif name == "list_schedules":
            out = self.store.list_schedules()
        elif name == "cancel_schedule":
            out = self.store.cancel_schedule(args["schedule_id"])
        elif name == "run_workflow":
            out = self.run_plan(self.store.get_workflow(args["name"]))
        elif name == "list_workflows":
            out = [{"name":x["name"],"steps":len(json.loads(x["steps_json"]))} for x in self.store.list_workflows()]
        else:
            raise PermissionError("Unknown tool")
        self.store.event("tool",{"tool":name,"args":args,"result":out})
        return out

    def _service(self,n,verb):
        if n not in self.services or not _valid_name(n):
            raise PermissionError("Service not approved")
        done = subprocess.run(["systemctl","--user",verb,n], capture_output=True,text=True,
                              timeout=10,check=False)
        return {"exit_code":done.returncode,"message":(done.stdout or done.stderr).strip()[:300]}

    def run_plan(self,steps):
        steps = validate_plan(steps, self.cfg.get("max_plan_steps",5))
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

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.thread.is_alive():
            self.thread.join(timeout=4)

    def _loop(self):
        while not self.stop_flag.wait(2):
            for kind, event in self.store.due():
                try:
                    if kind == "reminder":
                        self.on_reminder(event)
                    else:
                        result=self.tools.execute(event["tool"],{},from_plan=True)
                        self.store.event("scheduled_result",{"id":event["id"],"result":result})
                except Exception as exc:
                    LOG.exception("Scheduled event failed: %s",exc)
                    self.store.event("scheduler_error",{"event":event,"error":str(exc)})
