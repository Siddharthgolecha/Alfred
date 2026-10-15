"""System 2 policy: stage sensitive plans; perform no tool side effect before approval.

An approval is a local, one-time CLI action, *not* an open-microphone "yes".
The same policy applies to Needle direct calls and Qwen-generated plans.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass

from .models import QwenPlanResponseError
from .runtime import SCHEMA_BY_NAME, TOOL_SCHEMAS, validate_plan

LOG = logging.getLogger(__name__)

# Only read-only requests and simple reminder creation are eligible for voice execution.
# These operations may still be rejected by the tool registry at execution time.
VOICE_ALLOWED = frozenset({
    "get_system_status", "get_gpu_status", "list_services",
    "list_reminders", "list_schedules", "list_workflows", "list_automations",
    "create_reminder", "create_reminder_at"
})
SENSITIVE = frozenset({
    "control_service", "run_named_task", "run_workflow",
    "cancel_reminder", "schedule_every", "cancel_schedule",
    "create_daily_reminder", "watch_gpu_temperature", "pause_automation",
    "resume_automation", "cancel_automation"
})


def needs_approval(steps, save_as=None):
    return bool(save_as) or any(step["tool"] not in VOICE_ALLOWED for step in steps)


def preflight(steps, tools, max_steps=5):
    """Validate the full plan and concrete allowlists BEFORE any step executes."""
    validated = validate_plan(steps, max_steps, allow_cancellations=True)
    for step in validated:
        tool, args = step["tool"], step.get("args", {})
        if tool == "control_service" and args["name"] not in tools.services:
            raise PermissionError("Unknown/unapproved service")
        if tool == "run_named_task" and args["name"] not in tools.tasks:
            raise PermissionError("Unknown/unapproved named task")
    return validated


def concise_plan(steps):
    """Summarize actions without leaking full user content into speech/logs."""
    return ", ".join(step["tool"] for step in steps) or "(no actions)"


class Approvals:
    """Durable queue, atomic one-time claiming, never replay after interrupted action."""
    def __init__(self, store, ttl_seconds=180):
        self.store = store
        self.ttl = int(ttl_seconds)
        with self.store.lock, self.store.db:
            self.store.db.execute("""
                CREATE TABLE IF NOT EXISTS approvals(
                    id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    save_as TEXT,
                    result_json TEXT
                )
            """)

    def propose(self, steps, save_as=None):
        if not isinstance(steps, list) or (not steps and not save_as):
            raise ValueError("Nothing to approve")
        now = int(time.time())
        with self.store.lock, self.store.db:
            cur = self.store.db.execute(
                "INSERT INTO approvals(created_at,expires_at,status,steps_json,save_as) "
                "VALUES(?,?,?,?,?)", (now, now+self.ttl, "pending",
                                    json.dumps(steps, separators=(",",":")),save_as))
            return {"approval_id":cur.lastrowid, "expires_at":now+self.ttl,
                    "actions":concise_plan(steps), "save_as":save_as}

    def pending(self):
        now=int(time.time())
        with self.store.lock:
            rows=self.store.db.execute(
                "SELECT id,created_at,expires_at,steps_json,save_as FROM approvals "
                "WHERE status='pending' AND expires_at>? ORDER BY created_at",
                (now,)).fetchall()
        return [{"id":r["id"],"created_at":r["created_at"],"expires_at":r["expires_at"],
                 "actions":concise_plan(json.loads(r["steps_json"])),"save_as":r["save_as"]}
                for r in rows]

    def claim(self, approval_id):
        now=int(time.time())
        with self.store.lock, self.store.db:
            row=self.store.db.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row or row["status"]!="pending" or row["expires_at"]<=now:
                raise ValueError("Approval missing, expired, or already acted on")
            cur=self.store.db.execute(
                "UPDATE approvals SET status='running' WHERE id=? AND status='pending' AND expires_at>?",
                (approval_id,now))
            if cur.rowcount!=1:
                raise ValueError("Approval already claimed")
            return {"id":row["id"],"steps":json.loads(row["steps_json"]),
                    "save_as":row["save_as"]}

    def finish(self, approval_id, result=None, error=None):
        status="failed" if error else "complete"
        outcome={"error":str(error)[:300]} if error else {"result":result}
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE approvals SET status=?,result_json=? WHERE id=? AND status='running'",
                (status,json.dumps(outcome,default=str)[:10000],approval_id))

    def reject(self, approval_id):
        with self.store.lock, self.store.db:
            cur=self.store.db.execute(
                "UPDATE approvals SET status='rejected' WHERE id=? AND status='pending'",
                (approval_id,))
            return bool(cur.rowcount)


def parse_plan(plan, text, tools, max_steps):
    if not isinstance(plan,dict) or not isinstance(plan.get("steps"),list):
        raise ValueError("Reasoner response is not a valid plan")
    if any(k not in ("message","steps","save_as") for k in plan):
        raise ValueError("Unrecognized plan field")
    message=plan.get("message","")
    if not isinstance(message,str) or len(message)>1000:
        raise ValueError("Invalid reasoner message")
    steps=preflight(plan["steps"],tools,max_steps)
    save_as=plan.get("save_as") or None
    if save_as is not None:
        if not isinstance(save_as,str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{1,40}",save_as):
            raise ValueError("Invalid workflow name")
        if not re.search(r"\b(save|create|remember|store)\b.*\bworkflow\b",text,re.I):
            raise ValueError("Saving a workflow requires an explicit request")
        if not steps:
            raise ValueError("Cannot save empty workflow")
    return message,steps,save_as


@dataclass(frozen=True)
class VerifiedRead:
    """Python-authenticated returned read, never a model-authored claim."""
    tool: str
    output: object


class System2:
    """Single entry for Qwen plans, with explicit read-only fallback."""
    def __init__(self, qwen, tools, store, cfg):
        self.qwen=qwen
        self.tools=tools
        self.store=store
        self.cfg=cfg
        self.approvals=Approvals(store,cfg.get("approval_ttl_seconds",180))

    def execute_or_stage(self, steps, message="", save_as=None, allow_actions=True, force_approval=False):
        steps=preflight(steps,self.tools,self.cfg.get("max_plan_steps",5))
        if not allow_actions:
            if any(s["tool"] not in VOICE_ALLOWED or s["tool"] in
                   ("create_reminder","create_reminder_at") for s in steps) or save_as:
                # Never silently filter out dangerous steps and claim the command succeeded.
                return {"message":"That request needs an explicit actionable command.", "steps":[]}
        if save_as and any(s["tool"] in ("cancel_reminder","cancel_schedule",
                                      "cancel_automation","pause_automation",
                                      "resume_automation") for s in steps):
            raise ValueError("Saved workflows cannot contain cancellation actions")
        if force_approval or needs_approval(steps,save_as):
            proposal=self.approvals.propose(steps,save_as)
            self.store.event("approval_requested",{"id":proposal["approval_id"],
                         "actions":proposal["actions"]})
            return {"message":"Awaiting local approval. "+message[:300], "approval":proposal}
        if not steps:
            return {"message":message or "No tool action required","results":[]}
        results=self.tools.run_plan(steps)
        # Bind observations to *preflighted and actually returned* step names.
        # The planner's text and any unexecuted/skipped step are not evidence.
        # This opaque typed channel is consumed by Alfred, then removed before
        # showing JSON to users or passing anything back to a language model.
        from .cognitive.context import READS
        verified = tuple(
            VerifiedRead(step["tool"], value)
            for step, value in zip(steps, results)
            if step["tool"] in READS and not (
                isinstance(value,dict) and value.get("skipped") is True)
        ) if isinstance(results,list) and len(results)==len(steps) else ()
        return {"message":message or "Completed","results":results,
                "_verified_reads":verified}

    @staticmethod
    def _incomplete_plan():
        # Explicit failure metadata prevents a failed inference from being
        # recorded as a successful "no action" completion in the task ledger.
        return {"status": "incomplete", "error": "invalid_plan",
                "message": (
                    "I couldn't interpret that request as a valid plan, so no "
                    "actions were taken or scheduled. Please try again with a "
                    "specific action and time or interval."
                ), "results": []}

    def reason(self, utterance, allow_actions=True, *, context=None):
        # Tool schemas, not just tool names, are provided to the planner.
        tool_catalog=[s for s in TOOL_SCHEMAS if s["name"] not in
                      ("delegate_to_reasoner","run_workflow")]
        opts={"context":context} if context and (
            context.get("slots") or context.get("task_observations")
            or context.get("user_approved_facts")) else {}
        try:
            plan=self.qwen.complete(utterance, tool_catalog,
                                    allowed_services=self.tools.services,
                                    named_tasks=self.tools.tasks.keys(),**opts)
        except QwenPlanResponseError as exc:
            # Generation retries never confer authorization; a failed
            # structured response is a failed, non-executed plan.
            LOG.warning("System 2 plan unavailable: %s", exc)
            return self._incomplete_plan()
        try:
            message,steps,save_as=parse_plan(plan,utterance,self.tools,
                                            self.cfg.get("max_plan_steps",5))
        except (ValueError, PermissionError) as exc:
            LOG.warning("System 2 plan rejected by preflight: %s", exc)
            return self._incomplete_plan()
        outcome = self.execute_or_stage(steps,message,save_as,allow_actions)
        # Only a completed, read-only plan may generate a second explanation.
        # No output from this second inference is interpreted as a tool call.
        wants_explanation=bool(re.search(
            r"\b(why|explain|how|compare|interpret|mean|meaning)\b",
            utterance,flags=re.IGNORECASE))
        if (wants_explanation and outcome.get("results") and
                all(step["tool"] in VOICE_ALLOWED and
                    step["tool"]!="create_reminder" for step in steps)):
            try:
                outcome["message"]=self.qwen.explain(utterance,outcome["results"])
            except Exception as exc:
                LOG.warning("Post-tool explanation unavailable; retaining observed data: %s",exc)
        return outcome

    def direct(self, calls):
        """Needle calls are routed through exactly the same policy."""
        if not isinstance(calls,list):
            raise ValueError("Malformed Needle calls")
        if len(calls)>self.cfg.get("max_plan_steps",5):
            raise ValueError("Too many Needle calls")
        if any(c.get("name")=="delegate_to_reasoner" for c in calls):
            if len(calls)!=1:
                raise ValueError("Reasoner delegation must be a standalone call")
            raise ValueError("Call reason() with the original utterance")
        if len(calls)==1 and calls[0].get("name")=="run_workflow":
            workflow_name=calls[0].get("arguments",{}).get("name","")
            # Phase 4 graph workflows have locally approved immutable digests.
            # Stage the digest in the existing one-shot trusted CLI approval,
            # never as a remotely executable tool argument.
            try:
                entry=self.tools.workflow_engine.get(workflow_name,require_approved=False)
            except ValueError:
                entry=None
            if entry is not None:
                if entry["state"]!="approved":
                    raise PermissionError("Graph workflow requires local approval")
                item={"tool":"run_workflow","args":{
                    "name":workflow_name,"approved_digest":entry["digest"]}}
                proposal=self.approvals.propose([item])
                self.store.event("workflow_approval_requested",{
                    "id":proposal["approval_id"],"name":workflow_name})
                return {"message":"Awaiting local approval to start graph workflow",
                        "approval":proposal}
            # Preserve existing Phase 2 legacy linear saved workflows.
            saved=self.store.get_workflow(workflow_name)
            return self.execute_or_stage(saved, message=f"Run saved workflow {workflow_name}",
                                         force_approval=True)
        steps=[{"tool":c["name"],"args":c.get("arguments") or {}} for c in calls]
        return self.execute_or_stage(steps)

    def approve(self, approval_id):
        """Invoke ONLY from a trusted local CLI, not through the voice registry."""
        item=self.approvals.claim(approval_id)
        try:
            requested=item["steps"]
            if (item["save_as"] is None and len(requested)==1 and
                requested[0].get("tool")=="run_workflow" and
                isinstance(requested[0].get("args"),dict) and
                "approved_digest" in requested[0]["args"]):
                meta=requested[0]["args"]
                if set(meta)!={"name","approved_digest"}:
                    raise ValueError("Unrecognized graph approval metadata")
                graph=self.tools.workflow_engine.get(meta["name"])
                if graph["digest"]!=meta["approved_digest"]:
                    raise ValueError("Workflow changed since voice approval request")
                result=self.tools.workflow_engine.start(meta["name"])
                self.approvals.finish(approval_id,result=result)
                return result
            steps=preflight(requested,self.tools,self.cfg.get("max_plan_steps",5))
            if item["save_as"]:
                # Saving is a write: the plan is not executed by saving it.
                result=self.store.save_workflow(item["save_as"],steps)
                self.store.event("workflow_saved",result)
            else:
                result=self.tools.run_plan(steps,allow_cancellations=True)
            self.approvals.finish(approval_id,result=result)
            return result
        except Exception as exc:
            self.approvals.finish(approval_id,error=exc)
            raise
