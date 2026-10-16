# Cognitive layer

`alfred/cognitive/` holds everything that is model-adjacent but not model-controlled: typed
event contracts, the session ledger, scoped context, the Needle loop, the Needle → Qwen handoff
and the DeepSeek Harness adapter. The package docstring puts it as "model-agnostic,
non-executing task/session correlation".

| Module | Responsibility |
|---|---|
| `contracts.py` | Seven versioned Pydantic event types and a strict JSON codec |
| `sessions.py` | Append-only SQLite ledger with process ownership and recovery |
| `context.py` | Three-tier scoped context (`ContextManager`) |
| `needle_loop.py` | Bounded read-only feedback loop and `SessionSlots` |
| `escalation.py` | `EscalationEnvelope` for Needle → Qwen continuation |
| `harness_sdk.py` | `LocalHarness`, the adapter around the DeepSeek SDK |
| `harness_config.py`, `harness_audit.py` | Pinned plugin manifest and audit functions |
| `harness_bridge.py` | Validated projection of contracts for the Harness |
| `alfred_safe.patch.yml` | Cordis overlay that disables host-side plugins |

## Contracts

Every event in the ledger is a frozen Pydantic model (`ConfigDict(frozen=True, extra="forbid")`)
subclassing `Contract`. Serialised form is compact JSON with `kind` and `version` (currently
`1`). Decoding rejects unknown fields, wrong versions, duplicate keys, `NaN`/`Infinity`, booleans
masquerading as integers, and any payload over 2048 bytes (`Truncation.CONTRACT_BYTES`).

| Kind | Fields | Meaning |
|---|---|---|
| `task` (`TaskEnvelope`) | `session_id`, `task_id`, `source`, `input_chars`, `started_at` | Starts a task. `source` is `text`, `voice` or `reason`. Stores the input *length*, not the text. |
| `decision` | `engine`, `route`, `call_count`, `confidence` | `engine` is `needle3`, `qwen` or `local`. `route` is `direct`, `delegate`, `readonly`, `uncertain`, `rejected` or `shutdown`. |
| `observation` | `tool`, `outcome` | `outcome` is `returned`, `blocked`, `failed` or `staged`. No tool output. |
| `escalation` | `source_model`, `target_model`, `reason` | Needle (or local) to Qwen. Reason is `unsupported`, `delegated`, `explicit_reason`, `uncertain` or `incomplete`. |
| `approval` (`ApprovalReference`) | `approval_id` | Points at a row in `approvals`. |
| `completion` | `outcome`, `reason` | `outcome` is `completed`, `pending_approval`, `rejected` or `failed`. Reasons include `executed`, `no_action`, `needs_approval`, `unsafe_route`, `low_confidence`, `exception`, `disabled`, `shutdown`, `invalid_plan`. |
| `diagnostic` | `name`, `metrics` | Opt-in (`runtime.cognitive_diagnostics`). Integer metrics from a fixed allowlist (`latency_ms`, `input_chars`, …). |

No prompt, transcript, audio, tool argument or tool result is a field of any contract.

## Session ledger

`SessionLedger` stores the contracts in four tables inside the shared Alfred database. See
[Data model](data-model.md).

```{mermaid}
stateDiagram-v2
    [*] --> running: begin_task() writes TaskEnvelope
    running --> running: append(Decision / Observation / Escalation / ApprovalReference / Diagnostic)
    running --> completed: Completion(completed)
    running --> pending_approval: Completion(pending_approval)
    running --> rejected: Completion(rejected)
    running --> failed: Completion(failed)
    completed --> [*]
    pending_approval --> [*]
    rejected --> [*]
    failed --> [*]
```

Integrity rules enforced on every read and write:

- At most 64 events per task (`MAX_TASK_EVENTS`).
- The first event must be the matching `TaskEnvelope`; there may be only one.
- Nothing may follow a `Completion`; a closed task refuses appends.
- Events are immutable: a SQLite trigger aborts any `UPDATE` on `cognitive_events`.
- Cross-session corruption, a wrong event kind or a task/state mismatch raises `ValueError`.

### Ownership and recovery

Each running process records an owner token, PID and (on Linux) a `/proc/<pid>/stat` start-time
marker, which stops a reused PID from looking alive.

- Using a session owned by a **live** process raises `RuntimeError`.
- A session owned by a **dead** process requires explicit recovery with
  `alfred --resume-session SESSION_ID`.
- Recovery closes every still-running task as `Completion(failed, shutdown)`. It **never replays**
  a tool, because side effects may already have happened before the crash.
- Lease expiry by timestamp alone is deliberately not used; a slow but live process may be
  running a tool.
- `release_ownership()` on a clean shutdown removes only this instance's claims.
- On platforms without `/proc`, an existing PID is treated as live (fail closed).

## Context

`ContextManager` supplies bounded, verified, privacy-preserving data to Needle and Qwen. It has
no tool authority and does not use a model to summarise anything.

| Tier | Lifetime | Content | Written by |
|---|---|---|---|
| 1. Task observations | One task, in memory | Up to 3 verified read-only results, reduced to allowlisted numbers | Trusted `System2` result path only |
| 2. Session slots | TTL (default 120 s), in memory | `gpu_temperature`, `system_status`, `active_workflow` keyed by (owner, mode, session) | Same |
| 3. Approved facts | Until cleared; optional SQLite persistence | Up to 8 `key → short value` preferences per owner and mode | Local CLI only (`--memory-set`) |

Behaviour that matters:

- Pronouns like "check it again" resolve **locally** and only when exactly one fresh read-only
  slot is unambiguous. "Restart it" or "approve that" are rejected, never turned into a tool call.
- Metrics are filtered by `_bounded_metrics` (`available`, `gpu_temp_c`, `uptime_seconds`,
  `root_free_gib`). `list_services` is reduced to a count. Strings never pass through.
- `compact()` enforces a hard JSON budget (`runtime.context_budget_chars`, 256–3200). It drops
  oldest task observations first, then facts, then slots. It never cuts JSON mid-token.
- The command **"clear this session"** wipes slots and task observations without calling a model.
  Approved facts are retained.
- Facts with credential-like keys or values, control characters or shell-like content are
  rejected. This is a filter, not a proof against prompt injection. Facts are untrusted
  preferences, never policy or approvals.
- When fact persistence is on, the database file is set to mode `0600` and SQLite
  `secure_delete` is enabled. Neither erases backups, snapshots or old WAL data.
- `context_owner` is a configured namespace, not speaker identification.

## Needle loop

`NeedleAgentLoop` lets Needle see the *result* of a read-only call and decide on one more step.
It runs only for `direct` routes whose wording matches the multi-step pattern: one of `then`,
`afterwards`, `after that`, `next`, `depending on` or `based on` (`Patterns.MULTISTEP`). An
ordinary single request, such as "check GPU temperature", uses one Needle decision, one
execution, no feedback turn and no Qwen call.

A lock in `Alfred` serializes the whole `reset`, route, validated tools, feedback and finalize
sequence, so two authorized tasks cannot corrupt Needle's native conversation. Native history is
reset for each authorized command. Shutdown signals cancellation without taking that lock, and
the loop checks it before each decision, tool and feedback step. A native `complete()` call that
is already running cannot be interrupted.

Bounds, from `runtime.needle_max_decisions` and `runtime.needle_max_tools` (each configurable
from 1 to 3, default 3):

- Only `get_gpu_status`, `get_system_status` and `list_services` are read by the loop.
- Feedback to Needle is a dictionary of numbers (`_feedback_metrics`), never text.
- A repeated call fingerprint, a low confidence, a call count over budget, a `suppressed_calls`
  or `ungrounded` flag, or a cancel signal stops the loop.
- Any non-read-only call is handed to `System2.direct()` and the loop ends with stop reason
  `protected`. It is never fed back to Needle.
- A stop after earlier successful reads returns those reads as `partial`, not as a failure.

Stop reasons: `completed`, `protected`, `partial`, `uncertain`, `repeated`, `budget`, `unsafe`,
`cancelled`, `feedback_unavailable`.

The loop never calls `Needle.run()` because that would invoke Python callbacks outside Alfred's
approval policy. The first `Cactus.route()` resets the native conversation; follow-ups go
through `Cactus.route_feedback()`.

## Escalation envelope

See [Request flow](request-flow.md#3-needle-to-qwen-handoff) for the sequence. The contract
itself ([`escalation.py`](../../alfred/cognitive/escalation.py)):

- Version 1. Request 1–1200 characters. Serialised model context at most 3200 characters.
- `remaining_tools` is 0–5 (`MAX_HANDOFF_TOOLS`), counting Needle reads already done.
  `remaining_decisions` is 0–3.
- The total tool budget of a handoff is `min(runtime.max_plan_steps, 5)`. Needle reads already
  done use slots from the same total, and Qwen gets the remainder. With the default
  `needle_max_tools` of 3 and `max_plan_steps` of at least 5, Qwen can add at most two reads.
  A `max_plan_steps` of 6 to 10 does not raise the cap.
- If the completed prefix already exceeds the configured total, Alfred reports the original
  observations as `incomplete` with the reason `handoff_budget_exhausted`. It does not call
  Qwen, replay a read or lose the task history. `needle_max_decisions` is a separate bound on
  Needle feedback turns, not on Qwen.
- The envelope carries only this task's completed read names and bounded metrics. It does not
  forward the wider context slots or retained facts to Qwen or the Harness.
- Only read tools may be in the completed prefix. A completed write makes the task
  non-resumable (`PermissionError`).
- `reconcile(steps)` rejects over-budget plans, repeated signatures, and, when
  `allow_actions` is false, any non-read tool.

Reasons: `unsupported`, `delegated`, `uncertain`, `incomplete`, `explicit_reason`.
`uncertain`, `unsupported` and `incomplete` are read-only handoffs. Only `delegated` (Needle
chose `delegate_to_reasoner`) may produce actions, and those are still staged for local approval.

## Related

- [DeepSeek Harness](harness.md)
- [Security model](security-model.md)
- [Configuration reference](../reference/configuration.md)
