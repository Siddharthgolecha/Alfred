# Workflows

A workflow is a saved, approved graph of tool calls that Alfred can run on demand or on a
schedule. Graphs are plain JSON data. The engine never runs user code, shell fragments, network
calls or nested workflows.

Implementation: [`alfred/workflow_engine.py`](../../alfred/workflow_engine.py). All
management commands are CLI-only: see the [CLI reference](../reference/cli.md#workflows).

## Lifecycle

A **definition** is drafted and approved. An approved definition is run, and each **run** is
tracked separately.

```{mermaid}
stateDiagram-v2
    direction LR
    state "Definition" as def {
        [*] --> draft: --workflow-draft
        draft --> approved: --workflow-approve
        approved --> draft: re-draft
    }
    state "Run" as run {
        [*] --> queued: --workflow-start
        queued --> running: tick
        running --> completed
        running --> failed
        running --> cancelled: --workflow-cancel
        running --> interrupted: --workflow-reconcile
    }
    def --> run: approved only
```

A run stores a snapshot of the approved definition and its digest, so later edits never change a
run in progress. Drafting a changed graph returns the definition to `draft` and needs approval
again.

## Write a graph

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

This reads GPU status, then reads system status only if the GPU temperature is at least 75 °C.
It never shuts anything down. If `tegrastats` returns no temperature, the condition is false.

### Rules

| Item | Rule |
|---|---|
| Top-level keys | Exactly `version` (must be `1`), `name` and `steps`. |
| Names and step IDs | A letter, then up to 39 letters, digits, `_` or `-`. |
| Steps | 1 to 12. |
| Step keys | `id`, `tool`, `args`, `depends_on`, `when`, `retry`, `timeout_seconds`. |
| Dependencies | Must name **earlier** steps. Cycles and forward references are rejected before a draft is saved. |
| `when` | Compares one scalar top-level field of a dependency. The step it names must also be in `depends_on`. Operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`. |
| `retry` | `max_attempts` 1 to 3, `backoff_seconds` 0 to 300. Honoured **only for read-only tools**. |
| `timeout_seconds` | 5 to 120. Defaults to 15, or 75 for `run_named_task`. `run_named_task` needs at least 60 and `get_gpu_status` at least 6. |
| Result size | Stored results are limited to 5000 characters. |

A false condition marks the step **skipped**, and any step that depends on a skipped step is
skipped too.

### Allowed tools

| Group | Tools |
|---|---|
| Read-only | `get_system_status`, `get_gpu_status`, `list_services`, `list_reminders`, `list_schedules`, `list_automations`, `list_workflows` |
| Controlled | `control_service` and `run_named_task`, subject to the same allowlists as in plans |

These are rejected as graph steps: `run_workflow`, `delegate_to_reasoner`, `schedule_every`,
`create_daily_reminder`, `watch_gpu_temperature`, `cancel_automation`, `pause_automation`,
`resume_automation`, `cancel_schedule`, `cancel_reminder`, `create_reminder_at` and
`create_reminder`.

A nonzero return code from `run_named_task`, or a nonzero exit from `control_service`, fails the
step. Services and tasks run at most once.

## Run it

```bash
alfred --workflow-draft gpu_health.json      # validate and save as draft
alfred --workflow-list                       # see approval state
alfred --workflow-approve gpu_health         # approve this exact version

alfred --workflow-start gpu_health           # queue a run
alfred --workflow-tick                       # advance up to three ready steps
alfred --workflow-runs
alfred --workflow-status 1                   # durable results per step
```

`--workflow-draft` reads a local file of at most 40 000 bytes. The commands print JSON. Drafting
returns `name`, `state` (`draft`), the step count and the `digest`.

In normal operation you do not call `--workflow-tick`. The scheduler thread calls it every two
seconds, and each cycle advances at most **three** steps, so timers and the microphone stay
responsive. `--scheduler-only` does this without any microphone or model.

### Starting from voice

Saying "run gpu_health workflow" can make Needle propose `run_workflow`. That only **stages an
approval** carrying the graph digest. Run `alfred --pending`, then `alfred --approve ID`. Alfred
re-checks the digest before the run is queued, and a changed graph invalidates the request. See
[Approvals](approvals.md).

## Schedule a read-only graph

```bash
alfred --workflow-schedule gpu_health:10     # every 10 minutes
alfred --workflow-triggers
alfred --workflow-trigger-pause 1
alfred --workflow-trigger-resume 1
alfred --workflow-trigger-cancel 1
```

The argument is `NAME:MINUTES`, split on the last `:`. Triggers are limited to graphs that use
read-only tools, with an interval of 1 to 10080 minutes. A trigger:

- never overlaps its own active run,
- coalesces missed intervals instead of catching up,
- pauses automatically if the graph digest or its approval changes.

## Cancel and recover

```bash
alfred --workflow-cancel 1
alfred --workflow-reconcile 1
```

**Cancellation is cooperative.** It stops future steps. A step already executing finishes. The
engine does not kill a running call.

**`timeout_seconds` is an elapsed-time check after the tool returns**, not a preemptive kill. The
tools carry their own bounds: 10 s for `control_service`, 60 s for `run_named_task`. If the graph
budget was exceeded, the step fails and its dependants are not scheduled.

**Crash recovery never replays.** Steps are claimed transactionally in SQLite, so two Alfred
processes cannot take the same step. A step still marked `running` after a crash is not
restarted, even if it is read-only. Check by hand whether it had an effect, then run
`--workflow-reconcile RUN_ID`. That marks the stuck step and its run `interrupted`. It does not
replay or undo anything. It returns `{"changed": false}` if the run is not queued or running, or
has no `running` step. To repeat the work, start a fresh run.

## States

Run states:

| State | Meaning |
|---|---|
| `queued` | Waiting for the scheduler. |
| `running` | Steps are executing. |
| `completed` | Every step succeeded or was skipped. |
| `failed` | A step failed or was interrupted. |
| `cancelled` | Cancelled by you. |
| `interrupted` | Reconciled after a crash. |

Step states are `pending`, `running`, `retry_wait`, `succeeded`, `skipped`, `failed` and
`interrupted`.

## Privacy

The database and event log can hold tool arguments, task names and returned diagnostics. Protect
the SQLite file and your user account, and do not put secrets in tool arguments.
