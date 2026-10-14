"""Phase 4: durable, bounded, acyclic, allowlisted workflow graphs.

Every externally visible tool is validated *before* a draft is stored.
Execution is deterministic and local. This engine never evaluates code from
Qwen, imports user-supplied modules, or invokes a shell.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from .runtime import validate,check_condition

LOG=logging.getLogger(__name__)
_ID=re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
# Read-only retries can be repeated safely. No side-effect action is retried.
READ_ONLY=frozenset({
    "get_system_status","get_gpu_status","list_services","list_reminders",
    "list_schedules","list_automations","list_workflows"
})
# Never permit recursive/indirect execution, LLM delegation, or another
# workflow definition/scheduling action inside a workflow step.
FORBIDDEN=frozenset({
    "run_workflow","delegate_to_reasoner","schedule_every",
    "create_daily_reminder","watch_gpu_temperature","cancel_automation",
    "pause_automation","resume_automation","cancel_schedule",
    "cancel_reminder","create_reminder_at","create_reminder"
})
# Side effects are allowed only when present in existing explicit allowlists
# and the workflow version has been locally approved.
PERMITTED=READ_ONLY|frozenset({"control_service","run_named_task"})
MAX_STEPS=12
MAX_RUNS_PER_TICK=3
RESULT_LIMIT=5000


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True)


def _digest(spec):
    return hashlib.sha256(_canonical(spec).encode()).hexdigest()


def _result(value):
    """Limit storage and prevent an unbounded command result from bloating DB."""
    if isinstance(value,dict):
        result=value
    else:
        result={"items":value}
    encoded=_canonical(result)
    if len(encoded)>RESULT_LIMIT:
        return {"truncated":True,"preview":encoded[:RESULT_LIMIT-100]}
    return result


def validate_graph(spec,tools,max_steps=MAX_STEPS):
    """Validate a versioned DAG with stable IDs and explicit ancestor predicates."""
    if not isinstance(spec,dict) or set(spec)!={"version","name","steps"}:
        raise ValueError("Workflow must have exactly version, name, steps")
    if spec["version"]!=1 or type(spec["version"]) is not int:
        raise ValueError("Unsupported workflow schema version")
    if not isinstance(spec["name"],str) or not _ID.fullmatch(spec["name"]):
        raise ValueError("Invalid workflow name")
    nodes=spec["steps"]
    if not isinstance(nodes,list) or not 1<=len(nodes)<=min(MAX_STEPS,max_steps):
        raise ValueError("Workflow must contain 1..12 bounded steps")
    ids=set()
    normalized=[]
    for node in nodes:
        if not isinstance(node,dict) or set(node)-{
            "id","tool","args","depends_on","when","retry","timeout_seconds"
        }:
            raise ValueError("Unknown workflow step fields")
        ident=node.get("id")
        if not isinstance(ident,str) or not _ID.fullmatch(ident) or ident in ids:
            raise ValueError("Invalid or duplicate step id")
        ids.add(ident)
        name=node.get("tool")
        if not isinstance(name,str) or name in FORBIDDEN or name not in PERMITTED:
            raise ValueError("Workflow tool is not approved for graph execution")
        args=node.get("args",{})
        validate(name,args,allow_delegate=False)
        if name=="control_service":
            if args["name"] not in tools.services:
                raise PermissionError("Workflow service is not preapproved")
        if name=="run_named_task":
            task=tools.tasks.get(args["name"])
            if not isinstance(task,list) or not task or any(not isinstance(x,str) for x in task):
                raise PermissionError("Workflow named task is not preapproved")
        deps=node.get("depends_on",[])
        if not isinstance(deps,list) or len(deps)>MAX_STEPS or any(
            not isinstance(x,str) for x in deps) or len(set(deps))!=len(deps):
            raise ValueError("Invalid dependency list")
        retry=node.get("retry",{"max_attempts":1,"backoff_seconds":0})
        if not isinstance(retry,dict) or set(retry)!={"max_attempts","backoff_seconds"}:
            raise ValueError("Retry must contain max_attempts, backoff_seconds")
        attempts=retry["max_attempts"]
        backoff=retry["backoff_seconds"]
        if type(attempts) is not int or not 1<=attempts<=3 or (
            type(backoff) is not int or not 0<=backoff<=300
        ):
            raise ValueError("Retry bounds exceeded")
        if name not in READ_ONLY and attempts!=1:
            raise ValueError("Side-effecting tools cannot retry automatically")
        timeout=node.get("timeout_seconds",75 if name=="run_named_task" else 15)
        if type(timeout) is not int or not 5<=timeout<=120:
            raise ValueError("Timeout must be 5..120 seconds")
        # Tools impose their own subprocess timeouts; this is an elapsed time
        # budget evaluated after each bounded invocation (not thread kill).
        if name=="run_named_task" and timeout<60:
            raise ValueError("Named task timeout must cover existing 60s process bound")
        if name=="get_gpu_status" and timeout<6:
            raise ValueError("GPU read timeout must cover 4s tegrastats timeout")
        when=node.get("when")
        if when is not None:
            if not isinstance(when,dict) or set(when)!={"step","field","op","value"}:
                raise ValueError("Invalid condition; expected step,field,op,value")
            if not isinstance(when["step"],str) or not isinstance(when["field"],str):
                raise ValueError("Condition references must be strings")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",when["field"]):
                raise ValueError("Unsafe field selector")
            if when["op"] not in ("eq","neq","gt","gte","lt","lte"):
                raise ValueError("Invalid comparison")
            if type(when["value"]) not in (str,bool,int,float):
                raise ValueError("Condition value must be a scalar")
        normalized.append({
            "id":ident,"tool":name,"args":args,"depends_on":deps,"when":when,
            "retry":{"max_attempts":attempts,"backoff_seconds":backoff},
            "timeout_seconds":timeout
        })
    byid={node["id"]:node for node in normalized}
    # Strong forward-reference prohibition eliminates cycles and ambiguity,
    # while still allowing arbitrary fan-out, joins, and nested branches.
    seen=set()
    for node in normalized:
        deps=node["depends_on"]
        if any(x not in seen for x in deps):
            raise ValueError("Dependencies must reference earlier steps (DAG)")
        when=node["when"]
        if when:
            origin=when["step"]
            if origin not in seen or origin not in deps:
                raise ValueError("Condition must reference an earlier dependency")
        seen.add(node["id"])
    return {"version":1,"name":spec["name"],"steps":normalized}


class WorkflowEngine:
    def __init__(self,store,tools):
        self.store=store
        self.tools=tools
        with store.lock,store.db:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS workflow_graphs(
                    name TEXT PRIMARY KEY,
                    spec_json TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('draft','approved')),
                    approved_at INTEGER, updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS workflow_runs(
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL, digest TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                      ('queued','running','completed','failed','cancelled','interrupted')),
                    started_at INTEGER NOT NULL, finished_at INTEGER,
                    error TEXT);
                CREATE TABLE IF NOT EXISTS workflow_step_runs(
                    run_id INTEGER NOT NULL, step_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                      ('pending','running','retry_wait','succeeded','skipped','failed','interrupted')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT, error TEXT, elapsed_ms INTEGER,
                    PRIMARY KEY(run_id,step_id));
            """)
            # Running work is NOT auto-replayed after restart. Explicit
            # operator reconciliation is required; starting a second Alfred
            # process must not mistakenly interrupt an active first process.

    def reconcile_interrupted(self,runid):
        """Explicit local CLI acknowledgement after inspecting possible effects."""
        with self.store.lock,self.store.db:
            run=self.store.db.execute("SELECT state FROM workflow_runs WHERE id=?",(runid,)).fetchone()
            if run is None or run["state"] not in ("queued","running"):
                return {"changed":False}
            running=self.store.db.execute(
                "SELECT step_id FROM workflow_step_runs WHERE run_id=? AND state='running'",
                (runid,)).fetchall()
            if not running:
                return {"changed":False}
            self.store.db.execute(
                "UPDATE workflow_step_runs SET state='interrupted',"
                "error='Interrupted during execution; inspect effects manually' "
                "WHERE run_id=? AND state='running'",(runid,))
            self.store.db.execute(
                "UPDATE workflow_runs SET state='interrupted',finished_at=?,"
                "error='Possible in-flight effect; no automatic replay' WHERE id=?",
                (int(time.time()),runid))
        return {"changed":True,"interrupted_steps":[r["step_id"] for r in running]}

    def draft(self,spec):
        valid=validate_graph(spec,self.tools)
        encoded=_canonical(valid)
        stamp=int(time.time())
        with self.store.lock,self.store.db:
            self.store.db.execute(
                "INSERT INTO workflow_graphs(name,spec_json,digest,state,updated_at)"
                " VALUES(?,?,?,'draft',?) ON CONFLICT(name) DO UPDATE SET "
                "spec_json=excluded.spec_json,digest=excluded.digest,"
                "state='draft',approved_at=NULL,updated_at=excluded.updated_at",
                (valid["name"],encoded,_digest(valid),stamp))
        return {"name":valid["name"],"state":"draft","steps":len(valid["steps"]),
                "digest":_digest(valid)}

    def approve(self,name):
        if not isinstance(name,str) or not _ID.fullmatch(name):
            raise ValueError("Invalid workflow name")
        with self.store.lock,self.store.db:
            row=self.store.db.execute(
                "SELECT * FROM workflow_graphs WHERE name=?",(name,)).fetchone()
            if row is None:
                raise ValueError("Unknown workflow draft")
            # Revalidate against current allowlists before approval.
            spec=validate_graph(json.loads(row["spec_json"]),self.tools)
            if _digest(spec)!=row["digest"]:
                raise ValueError("Workflow definition hash mismatch")
            self.store.db.execute(
                "UPDATE workflow_graphs SET state='approved',approved_at=? WHERE name=?",
                (int(time.time()),name))
        return {"name":name,"state":"approved","digest":row["digest"]}

    def get(self,name,require_approved=True):
        with self.store.lock:
            row=self.store.db.execute(
                "SELECT * FROM workflow_graphs WHERE name=?",(name,)).fetchone()
        if row is None or (require_approved and row["state"]!="approved"):
            raise ValueError("Workflow missing or not locally approved")
        spec=json.loads(row["spec_json"])
        validate_graph(spec,self.tools)
        if row["digest"]!=_digest(spec):
            raise ValueError("Workflow digest mismatch")
        return {"spec":spec,"state":row["state"],"digest":row["digest"]}

    def definitions(self):
        with self.store.lock:
            rows=self.store.db.execute(
                "SELECT name,digest,state,approved_at,updated_at FROM workflow_graphs ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def start(self,name):
        entry=self.get(name)
        spec=entry["spec"]
        now=int(time.time())
        with self.store.lock,self.store.db:
            # No future edits affect this run's immutable snapshot.
            current=self.store.db.execute(
                "SELECT digest,state FROM workflow_graphs WHERE name=?",(name,)).fetchone()
            if current["digest"]!=entry["digest"] or current["state"]!="approved":
                raise ValueError("Workflow was modified while starting")
            cur=self.store.db.execute(
                "INSERT INTO workflow_runs(name,digest,spec_json,state,started_at)"
                " VALUES(?,?,?,'queued',?)",
                (name,entry["digest"],_canonical(spec),now))
            runid=cur.lastrowid
            self.store.db.executemany(
                "INSERT INTO workflow_step_runs(run_id,step_id,state)"
                " VALUES(?,?,'pending')",
                [(runid,node["id"]) for node in spec["steps"]])
        self.store.event("workflow_started",{"run_id":runid,"name":name})
        return {"run_id":runid,"name":name,"state":"queued"}

    def cancel(self,runid):
        if type(runid) is not int or runid<1:
            raise ValueError("Invalid run ID")
        with self.store.lock,self.store.db:
            cur=self.store.db.execute(
                "UPDATE workflow_runs SET state='cancelled',finished_at=?"
                " WHERE id=? AND state IN ('queued','running')",(int(time.time()),runid))
            if cur.rowcount:
                self.store.db.execute(
                    "UPDATE workflow_step_runs SET state='skipped',error='Cancelled'"
                    " WHERE run_id=? AND state IN ('pending','retry_wait')",(runid,))
        # A running tool may still finish: no hard, unsafe thread termination.
        return {"run_id":runid,"cancelled":bool(cur.rowcount)}

    def details(self,runid):
        with self.store.lock:
            row=self.store.db.execute("SELECT * FROM workflow_runs WHERE id=?",(runid,)).fetchone()
            if row is None:
                raise ValueError("Unknown workflow run")
            steps=self.store.db.execute(
                "SELECT step_id,state,attempts,retry_at,error,elapsed_ms,result_json"
                " FROM workflow_step_runs WHERE run_id=? ORDER BY rowid",(runid,)).fetchall()
        return {**{k:row[k] for k in ("id","name","digest","state","started_at","finished_at","error")},
                "steps":[{**{k:v for k,v in dict(s).items() if k!="result_json"},
                         "result":json.loads(s["result_json"]) if s["result_json"] else None}
                         for s in steps]}

    def list_runs(self,limit=20):
        with self.store.lock:
            rows=self.store.db.execute(
                "SELECT id,name,state,started_at,finished_at,error FROM workflow_runs"
                " ORDER BY id DESC LIMIT ?",(min(100,max(1,int(limit))),)).fetchall()
        return [dict(r) for r in rows]

    def _next(self):
        """Atomically claim one ready/skippable step from each current run."""
        now=int(time.time())
        with self.store.lock,self.store.db:
            runs=self.store.db.execute(
                "SELECT * FROM workflow_runs WHERE state IN ('queued','running')"
                " ORDER BY id LIMIT 100").fetchall()
            for run in runs:
                spec=json.loads(run["spec_json"])
                rows=self.store.db.execute(
                    "SELECT * FROM workflow_step_runs WHERE run_id=?",(run["id"],)).fetchall()
                states={r["step_id"]:r for r in rows}
                if any(r["state"]=="running" for r in rows):
                    # Another worker may be executing. Never run another step
                    # until the active step completes or is reconciled.
                    continue
                if any(r["state"] in ("failed","interrupted") for r in rows):
                    self.store.db.execute(
                        "UPDATE workflow_runs SET state='failed',finished_at=?,"
                        "error='A workflow step failed' WHERE id=? AND state IN ('queued','running')",
                        (now,run["id"]))
                    continue
                if all(r["state"] in ("succeeded","skipped") for r in rows):
                    self.store.db.execute(
                        "UPDATE workflow_runs SET state='completed',finished_at=?"
                        " WHERE id=? AND state IN ('queued','running')",(now,run["id"]))
                    continue
                for node in spec["steps"]:
                    record=states[node["id"]]
                    if record["state"] not in ("pending","retry_wait") or record["retry_at"]>now:
                        continue
                    deps=node["depends_on"]
                    if any(states[d]["state"] not in ("succeeded","skipped") for d in deps):
                        continue
                    # Propagate skip through joins: a dependent node cannot
                    # accidentally act on a skipped prerequisite.
                    if any(states[d]["state"]=="skipped" for d in deps):
                        self.store.db.execute(
                            "UPDATE workflow_step_runs SET state='skipped',error='Dependency skipped'"
                            " WHERE run_id=? AND step_id=? AND state IN ('pending','retry_wait')",
                            (run["id"],node["id"]))
                        return ("skipped",run["id"],node,None)
                    pred=node["when"]
                    if pred:
                        p=states[pred["step"]]
                        prior=json.loads(p["result_json"]) if p["result_json"] else {}
                        # check_condition indexes a list. Reuse the existing
                        # scalar-only predicate and never evaluate expressions.
                        flat={"step":0,"field":pred["field"],"op":pred["op"],"value":pred["value"]}
                        if not check_condition(flat,[prior]):
                            self.store.db.execute(
                                "UPDATE workflow_step_runs SET state='skipped',error='Condition false'"
                                " WHERE run_id=? AND step_id=? AND state IN ('pending','retry_wait')",
                                (run["id"],node["id"]))
                            return ("skipped",run["id"],node,None)
                    cur=self.store.db.execute(
                        "UPDATE workflow_step_runs SET state='running',attempts=attempts+1"
                        " WHERE run_id=? AND step_id=? AND state IN ('pending','retry_wait')",
                        (run["id"],node["id"]))
                    if not cur.rowcount:
                        continue
                    self.store.db.execute(
                        "UPDATE workflow_runs SET state='running' WHERE id=? AND state='queued'",
                        (run["id"],))
                    return ("run",run["id"],node,record["attempts"]+1)
        return None

    def tick(self,max_actions=MAX_RUNS_PER_TICK):
        outcomes=[]
        for _ in range(min(MAX_RUNS_PER_TICK,max(1,int(max_actions)))):
            item=self._next()
            if not item:
                break
            kind,runid,node,attempt=item
            if kind=="skipped":
                outcomes.append({"run_id":runid,"step":node["id"],"state":"skipped"})
                continue
            started=time.monotonic()
            error=None
            result=None
            try:
                # Revalidate concrete permissions before *every* action.
                validate_graph({"version":1,"name":"runtime_check",
                                "steps":[{**node,"depends_on":[],"when":None}]},
                               self.tools,max_steps=1)
                result=_result(self.tools.execute(node["tool"],node["args"],from_plan=True))
                # A subprocess or systemctl failure is a failed workflow step,
                # even though the tool returned a structured response.
                if node["tool"]=="run_named_task" and result.get("returncode")!=0:
                    raise RuntimeError("Approved named task exited nonzero")
                if node["tool"]=="control_service" and (
                    not isinstance(result.get("result"),dict) or
                    result["result"].get("exit_code")!=0
                ):
                    raise RuntimeError("Approved systemctl action failed")
                if (time.monotonic()-started)>node["timeout_seconds"]:
                    raise TimeoutError("Tool exceeded its configured wall-clock budget")
            except Exception as exc:
                error=str(exc)[:500]
            elapsed=round((time.monotonic()-started)*1000)
            with self.store.lock,self.store.db:
                parent=self.store.db.execute(
                    "SELECT state FROM workflow_runs WHERE id=?",(runid,)).fetchone()
                if parent is None or parent["state"]=="cancelled":
                    # Tool may have completed before cancel; never claim reversal.
                    self.store.db.execute(
                        "UPDATE workflow_step_runs SET state='interrupted',error=?"
                        " WHERE run_id=? AND step_id=? AND state='running'",
                        ("Run cancelled while tool was active",runid,node["id"]))
                    outcomes.append({"run_id":runid,"step":node["id"],"state":"cancelled"})
                    continue
                retry=node["retry"]
                if error and node["tool"] in READ_ONLY and attempt<retry["max_attempts"]:
                    state="retry_wait"
                    retry_at=int(time.time())+max(1,retry["backoff_seconds"]*attempt)
                else:
                    state="failed" if error else "succeeded"
                    retry_at=0
                self.store.db.execute(
                    "UPDATE workflow_step_runs SET state=?,retry_at=?,result_json=?,"
                    "error=?,elapsed_ms=? WHERE run_id=? AND step_id=? AND state='running'",
                    (state,retry_at,_canonical(result) if result is not None else None,
                     error,elapsed,runid,node["id"]))
            outcomes.append({"run_id":runid,"step":node["id"],"state":state,
                             "error":error})
            self.store.event("workflow_step",outcomes[-1])
        # Completion is finalized on the next scheduler tick.
        return outcomes
