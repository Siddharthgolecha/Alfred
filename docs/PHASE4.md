# Phase 4 — Durable, bounded workflow graphs

Phase 4 extends Alfred's existing Phase 2 one-shot/linear plans and Phase 3 reminders with a **locally approved deterministic DAG workflow engine**. It is developed from current `main`; Jetson-specific SSD and meeting features on `intrinsic_jetson` remain separate.

## Capabilities

- Typed JSON version-1 DAGs with **1–12 steps**, stable IDs, earlier-step dependencies, fan-out and joins. Cycles and forward references are rejected before a draft can be saved.
- Conditions compare a scalar **top-level field** of an earlier dependency using `eq`, `neq`, `gt`, `gte`, `lt` or `lte`. False conditions mark a step skipped; any dependent step is also skipped.
- Retries (up to three attempts, bounded backoff) are **only available for read-only tools**. Service actions and named tasks are executed at most once. A nonzero return code counts as failure.
- SQLite persists workflow definitions, approvals, immutable version hashes, runs, individual step states, retries, results and errors. A process restart does not replay an uncertain in-flight step.
- Dedicated CLI-only draft/approve/start/cancel/reconcile actions, using your existing OS account permissions; Qwen/Needle never receive shell access.
- Approved, **read-only-only** DAGs can be run on a recurring minute interval. A changed definition or revoked approval pauses its recurring trigger; missed intervals are coalesced.
- The existing Alfred scheduler advances at most **three steps per cycle**, allowing it to serve timers and microphone requests. The `--scheduler-only` mode works without Cactus models, Bluetooth or a running Qwen server.

## Example

Create `gpu_health.json`:

```json
{
  "version": 1,
  "name": "gpu_health",
  "steps": [
    {
      "id": "measure",
      "tool": "get_gpu_status",
      "args": {},
      "depends_on": [],
      "retry": {"max_attempts": 2, "backoff_seconds": 2},
      "timeout_seconds": 10
    },
    {
      "id": "system_if_hot",
      "tool": "get_system_status",
      "args": {},
      "depends_on": ["measure"],
      "when": {"step": "measure", "field": "gpu_temp_c", "op": "gte", "value": 75}
    }
  ]
}
```

This measures GPU status, then checks overall system information only if the reported GPU temperature is at least 75°C. It **does not automatically shut down the machine**, execute a script or invent a reading. If `tegrastats` returns no temperature, the condition is false.

## CLI

```bash
# After checking out feat/phase4-durable-workflow-engine:
python -m alfred --workflow-draft gpu_health.json
python -m alfred --workflow-list
python -m alfred --workflow-approve gpu_health

# Trusted local terminal can queue an approved graph immediately:
python -m alfred --workflow-start gpu_health

# Scheduler handles one or more steps each interval:
python -m alfred --workflow-tick
python -m alfred --workflow-runs
python -m alfred --workflow-status 1

# Cancellation stops future steps; an active call may finish before stopping:
python -m alfred --workflow-cancel 1

# IMPORTANT: only after manually verifying a possibly interrupted side effect:
python -m alfred --workflow-reconcile 1

# Schedule a read-only graph every 10 minutes:
python -m alfred --workflow-schedule gpu_health:10
python -m alfred --workflow-triggers
python -m alfred --workflow-trigger-pause 1
python -m alfred --workflow-trigger-resume 1
python -m alfred --workflow-trigger-cancel 1

# Background execution; works with no microphone:
python -m alfred --scheduler-only
```

**Voice-started graph runs** continue through Phase 2's one-time terminal approval. Saying "run gpu_health workflow" may cause Needle to propose `run_workflow` but must not immediately start it. Inspect `python -m alfred --pending`, then use `--approve ID` locally; the graph definition digest is checked again before the run is queued. A changed graph invalidates the pending request.

## Durable state, recovery and limits

A run records a **snapshot** of the approved definition and its digest. Later edits don't change running executions. A revised definition returns to draft and requires local approval again.

All invocations are claimed transactionally in SQLite, preventing concurrent Alfred processes from claiming the same step simultaneously. A step still marked `running` after a process crash is **not restarted automatically**, even if read-only. Inspect side effects before invoking `--workflow-reconcile RUN_ID`, which marks the run interrupted; it does not replay or roll back any actions. To repeat work, explicitly start a fresh approved run.

Cancellation is cooperative. A running Python call or approved subprocess is not force-killed by the graph scheduler. The underlying service-action subprocess already has a 10-second bound and a named task has a 60-second bound; the graph's `timeout_seconds` is an **elapsed-time check after tool return**, not a guaranteed preemptive interruption. If the graph budget is exceeded, the step fails and no more dependent actions are scheduled.

Only existing preapproved user services and argv-listed tasks can be executed, and even those must be explicitly locally approved as a workflow before launch. The graph engine does not execute user-supplied Python, shell fragments, dynamic module imports, network connections or nested workflows. Recursive workflows, model delegation, timers and modifications to unrelated automation rules are explicitly forbidden as graph steps.

Recurring triggers are **read-only**, with 1–10080-minute intervals. They skip catch-up floods, don't overlap a prior active run of their own trigger, and pause if the graph version changes. A temperature check that lacks a valid reading cannot silently become a condition that executes a mutating step.

The database and its local event log can contain tool arguments, user-defined script names and returned diagnostic data. Protect the SQLite file and the local user account. Use narrow system permissions, and avoid entering sensitive secrets as tool arguments.

## Compatibility and validation

Phase 1 microphone capture, Phase 2 local approvals and legacy linear workflows, and Phase 3 automations are unchanged. This branch adds a new `alfred/workflow_engine.py` module and uses the existing scheduler, `Store`, and tool allowlists.

Offline CI tests run on Python 3.10 and Python 3.12, verifying DAG validation, successful and skipped branches, failure codes, retries, crash recovery, version invalidation, recurring scheduling and old workflow support. Real Jetson load/concurrency, actual `systemctl --user` behavior and model routing still need on-device validation.
