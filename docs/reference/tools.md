# Tool reference

Alfred exposes 21 tools. They are defined in `TOOL_SCHEMAS` in
[`alfred/runtime.py`](../../alfred/runtime.py) and handled by the `Tools` class. Needle sees these
schemas. Qwen plans against them. Neither can call anything outside this list.

Administrative actions (`--approve`, `--reject`, `--pending`, `--memory-*`, `--workflow-*`) are
**not** tools. They exist only on the [CLI](cli.md).

## Execution tiers

The tier comes from two sets in [`alfred/system2.py`](../../alfred/system2.py).

| Tier | Meaning |
|---|---|
| **Direct** | Member of `VOICE_EXECUTABLE_TOOLS` (9 tools). Runs immediately when the plan contains only these. |
| **Approval** | Member of `APPROVAL_REQUIRED_TOOLS` (11 tools). Staged as a pending approval and run only after `alfred --approve ID`. |
| **Orchestrator** | `delegate_to_reasoner`. Handled by the router. `Tools.execute` rejects it. |

Any plan that contains a tool outside the direct set, or that is being saved as a workflow,
needs approval. See [Request flow](../architecture/request-flow.md#4-approval).

When a request is handed off in a read-only context, `create_reminder` and `create_reminder_at`
are refused as well, with a message asking for an explicit command.

## Argument rules

Every tool takes an object with exactly the listed arguments. Unknown or missing arguments, wrong
types and out-of-range values raise `ValueError`. Strings are limited to 240 characters. Integers
must be real integers (not booleans or floats).

## System

| Tool | Tier | Arguments | Returns |
|---|---|---|---|
| `get_system_status` | Direct | none | Load averages, memory in MiB, free root disk in GiB, uptime in seconds. |
| `get_gpu_status` | Direct | none | `available`, per-sensor `temperatures_c`, `gpu_temp_c` and a truncated raw `tegrastats` line. |
| `list_services` | Direct | none | `is-active` result for each name in `runtime.allowed_services`. |
| `control_service` | Approval | `name` (string), `action` (`start` or `stop`) | `systemctl --user <action> <name>` exit code and message. `name` must be in `runtime.allowed_services`. |
| `run_named_task` | Approval | `name` (string) | Return code, last output characters and stderr. `name` must be in `runtime.named_tasks`. 60 s timeout. |

## Reminders

| Tool | Tier | Arguments | Notes |
|---|---|---|---|
| `create_reminder` | Direct | `message`, `delay_minutes` (1 to 10080) | One-shot reminder in the `reminders` table. |
| `create_reminder_at` | Direct | `message`, `when` | `when` is ISO-8601 with an offset or `Z`. Must be in the future and within five years. Message is 1 to 240 characters. |
| `create_daily_reminder` | Approval | `message`, `local_time` (`HH:MM`), `timezone` (IANA name) | Recurring. The next occurrence is computed in that timezone. |
| `list_reminders` | Direct | none | Pending reminders, soonest first, at most 100. |
| `cancel_reminder` | Approval | `reminder_id` (1 to 1000000000) | Cancels a pending reminder. |

## Automations

See the [Automations guide](../guides/automations.md).

| Tool | Tier | Arguments | Notes |
|---|---|---|---|
| `watch_gpu_temperature` | Approval | `threshold_c` (20 to 110), `interval_minutes` (1 to 1440), `cooldown_minutes` (1 to 10080) | Recurring read-only check. Alert hysteresis is fixed at 3 °C. |
| `list_automations` | Direct | none | Active and paused jobs, newest first, at most 200. |
| `pause_automation` | Approval | `automation_id` | Active to paused. |
| `resume_automation` | Approval | `automation_id` | Paused to active. The next time is recomputed. |
| `cancel_automation` | Approval | `automation_id` | Active or paused to cancelled. Undelivered alerts from the job are deleted. |

## Schedules

| Tool | Tier | Arguments | Notes |
|---|---|---|---|
| `schedule_every` | Approval | `tool` (`get_gpu_status` or `get_system_status`), `interval_minutes` (1 to 10080) | Runs a read-only tool on an interval. Missed periods are skipped, not replayed. |
| `list_schedules` | Direct | none | Active schedules. |
| `cancel_schedule` | Approval | `schedule_id` | Deactivates a schedule. |

## Workflows

See the [Workflows guide](../guides/workflows.md).

| Tool | Tier | Arguments | Notes |
|---|---|---|---|
| `run_workflow` | Approval | `name` | Runs a saved workflow. Cannot appear *inside* a plan. |
| `list_workflows` | Direct | none | Saved workflow names and step counts. |

## Reasoning

| Tool | Tier | Arguments | Notes |
|---|---|---|---|
| `delegate_to_reasoner` | Orchestrator | `request` | Asks Qwen to explain or plan. Not allowed inside a plan. |

## Plan structure

A plan is a list of at most `runtime.max_plan_steps` steps. Each step has `tool`, optional `args`
and an optional `when` condition:

```json
{
  "tool": "get_gpu_status",
  "args": {}
}
```

```json
{
  "tool": "create_reminder",
  "args": { "message": "Cool down", "delay_minutes": 10 },
  "when": { "step": 0, "field": "gpu_temp_c", "op": "gt", "value": 75 }
}
```

`when` must contain exactly `step`, `field`, `op` and `value`. `step` is the index of an
**earlier** step. `op` is one of `eq`, `neq`, `gt`, `gte`, `lt`, `lte`. `field` is a top-level
key of that step's result. `value` is a string, number or boolean. If the condition is false,
missing or has incompatible types, the step is skipped and recorded as `{"skipped": true}`.

Additional plan rules:

- `run_workflow` and `delegate_to_reasoner` are rejected inside a plan.
- `cancel_reminder` and `cancel_schedule` are rejected unless the caller allows cancellations.
- A plan that is saved as a workflow cannot contain `cancel_reminder`, `cancel_schedule`,
  `cancel_automation`, `pause_automation` or `resume_automation`.
- Services and named tasks are checked against the allowlists *before* any step runs.

Every executed tool writes a `tool` row to the `events` table.
