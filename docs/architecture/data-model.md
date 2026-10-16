# Data model

Alfred keeps all state in one SQLite database, `runtime.database` (default
`~/.local/share/alfred/alfred.db`). The connection is opened with `check_same_thread=False`,
guarded by a shared `threading.RLock` (`Store.lock`), and uses WAL journaling. Each subsystem
creates its own tables with `CREATE TABLE IF NOT EXISTS` when it is constructed. There is no
central migration runner.

## Entity overview

```{mermaid}
erDiagram
    cognitive_sessions ||--o{ cognitive_tasks : has
    cognitive_sessions ||--o| cognitive_session_owners : "owned by"
    cognitive_tasks ||--o{ cognitive_events : records
    cognitive_events }o..o| approvals : "approval contract points at"

    automation_jobs ||--o{ automation_outbox : delivers

    workflow_graphs ||--o{ workflow_runs : "run of (name, digest)"
    workflow_graphs ||--o{ workflow_triggers : "scheduled by"
    workflow_runs ||--o{ workflow_step_runs : contains
    workflow_triggers }o..o| workflow_runs : last_run_id

    reminders
    schedules
    workflows
    events
    approvals
    context_approved_facts
    meeting_cleanup_operations
```

Dotted lines are logical references with no foreign-key constraint. Only the `cognitive_*` tables
declare `REFERENCES`.

## Tables by subsystem

### Core (`runtime.Store`)

| Table | Purpose | Key columns |
|---|---|---|
| `reminders` | One-shot relative reminders | `message`, `due_at`, `status` (`pending`, `delivered`, `cancelled`) |
| `schedules` | Recurring read-only status polls | `tool`, `interval_minutes`, `due_at`, `active` |
| `workflows` | Legacy linear named workflows | `name` (PK), `steps_json` |
| `events` | Append-only audit log | `at`, `category`, `detail` (JSON, truncated to 6000 chars) |

`Store.due()` claims due reminders and schedules inside one transaction. Reminders flip to
`delivered` before the callback runs. Schedules advance `due_at` by whole intervals so a reboot
does not trigger a flood of missed runs.

Event categories written by the code include `utterance`, `route`, `route_uncertain`,
`routing_adjustment`, `tool`, `scheduled_result`, `scheduler_error`, `approval_requested`,
`workflow_approval_requested`, `workflow_saved`, `cognitive_handoff`, `reminder`,
`automation_tick_error` and `workflow_tick_error`.

### Approvals (`system2.Approvals`)

| Column | Notes |
|---|---|
| `id` | Integer primary key. This is the ID passed to `--approve` |
| `created_at`, `expires_at` | Epoch seconds; lifetime is `runtime.approval_ttl_seconds` |
| `status` | `pending`, `running`, `complete`, `failed`, `rejected` |
| `steps_json` | The staged plan |
| `save_as` | Workflow name when the plan is to be saved rather than run |
| `result_json` | Result or error, truncated to 10000 characters |

### Cognitive ledger (`cognitive.sessions`)

| Table | Purpose |
|---|---|
| `cognitive_sessions` | `id`, `started_at`, `last_active_at` |
| `cognitive_session_owners` | One row per session: `owner_token`, `owner_pid`, `owner_started` |
| `cognitive_tasks` | `id`, `session_id`, `source`, `started_at`, `state` |
| `cognitive_events` | `id`, `session_id`, `task_id`, `at`, `kind`, `contract_json` |

Indexes: `(task_id, id)` on events and `(session_id, started_at)` on tasks. A trigger,
`cognitive_events_immutable`, aborts any `UPDATE` on `cognitive_events`. `prune(before_epoch)`
deletes finished sessions with no live owner, but nothing in Alfred calls it automatically.
`cognitive_tasks.state` is `running` until a
`Completion` event sets it to the outcome (`completed`, `pending_approval`, `rejected`,
`failed`).

### Automations (`automations.Automations`)

| Table | Purpose | Constraints |
|---|---|---|
| `automation_jobs` | Absolute reminders, daily reminders, GPU temperature watches | `status` in `active`, `paused`, `completed`, `cancelled`; fields `next_at`, `interval_minutes`, `timezone`, `local_time`, `armed`, `last_value`, `last_triggered_at`, `last_error` |
| `automation_outbox` | Notifications awaiting delivery | `status` in `pending`, `sending`, `delivered`; `UNIQUE(job_id, due_at)`; `attempts`, `retry_at`, `lease_until` |

Indexes: `idx_auto_jobs(status, next_at)` and `idx_auto_outbox(status, retry_at)`. The outbox
gives **at-least-once** delivery: a crash between delivery and acknowledgement can repeat a
notification.

### Workflow graphs (`workflow_engine.WorkflowEngine`)

| Table | Purpose | State values |
|---|---|---|
| `workflow_graphs` | Versioned graph definitions with a content `digest` | `draft`, `approved` |
| `workflow_runs` | One execution; stores the spec it started with | `queued`, `running`, `completed`, `failed`, `cancelled`, `interrupted` |
| `workflow_step_runs` | Per-step status, `attempts`, `retry_at`, `result_json`, `elapsed_ms`; PK `(run_id, step_id)` | `pending`, `running`, `retry_wait`, `succeeded`, `skipped`, `failed`, `interrupted` |
| `workflow_triggers` | Recurring schedule for a read-only graph | `active`, `paused`, `cancelled` |

Runs and triggers record the `digest` they were created from, so a graph edited after approval
cannot silently change what an existing approval or trigger refers to. See
[Workflows](../guides/workflows.md).

### Context (`cognitive.context`)

| Table | Created when | Purpose |
|---|---|---|
| `context_approved_facts` | `context_persistence_enabled` is true | `(owner, mode, key)` PK, `value`, `provenance` (must be `user_approved`), `saved_at` |

Enabling persistence also sets the database file and its `-wal`/`-shm` siblings to mode `0600`,
refuses a symlink or a file you do not own, and sets `PRAGMA secure_delete=ON`.

### Meeting cleanup (`meeting_cleanup`)

| Table | Purpose |
|---|---|
| `meeting_cleanup_operations` | `operation_id` PK, `session_id`, `task_id`, `owner`, `meeting_id`, `retained`, `state` (`starting`, `recording`, `stopping`, `purge_pending`, `rollback_pending`, `stopped_retained`) |

A partial unique index on `meeting_id` stops two operations from claiming the same meeting. Only
opaque IDs are stored, never audio, transcripts or paths.

## Inspecting the database

```bash
sqlite3 ~/.local/share/alfred/alfred.db '.tables'
sqlite3 ~/.local/share/alfred/alfred.db \
  "select id,status,datetime(expires_at,'unixepoch') from approvals order by id desc limit 5"
```

Prefer the CLI (`--pending`, `--automations`, `--workflow-list`, `--workflow-runs`) over editing
rows by hand. Several invariants, such as approval claiming and run states, rely on code paths
that raw SQL bypasses.

## Backup and retention

- Stop Alfred, or use `sqlite3 ... ".backup"`, before copying. Copying the main file alone while
  WAL is active can lose recent writes.
- The `events` table is not pruned automatically.
- Removing the database discards pending approvals, schedules and workflow state. Nothing is
  replayed from the ledger.
