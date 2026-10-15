# Phase 5 P0: cognitive contracts and correlated session ledger (issue #12)

This is a **data contract**, not a new planner, executor, permission, or DeepSeek harness. `Tools`, `System2`, `Approvals`, and `WorkflowEngine` remain the only execution/approval authorities. Model behavior and the direct status route are unchanged.

## Version 1 contracts

`alfred/cognitive/contracts.py` defines a strict, JSON-serializable, immutable contract vocabulary. Every object has `kind` and `version:1`. Unknown properties, duplicate JSON keys, malformed types, nonfinite numbers, future schema versions, and serialized events larger than **2,048 UTF-8 bytes** are rejected. Identifiers are randomly generated 32-character lowercase hex UUIDs (UUID4); incoming session IDs must already exist.

| Kind | Fields | Permitted content |
|---|---|---|
| `task` (`TaskEnvelope`) | session_id, task_id, source, input_chars, started_at | `text/voice/reason`, bounded count and UTC seconds |
| `decision` (`Decision`) | engine, route, call_count, confidence | enumerated engines/routes and normalized confidence, **not prompts** |
| `observation` (`Observation`) | tool, outcome | tool *name* and `returned/blocked/failed/staged`; **no arguments/results** |
| `escalation` (`Escalation`) | source_model, target_model, reason | enumerated model handoff metadata |
| `approval` (`ApprovalReference`) | approval_id | existing one-shot approval ID; **not approval content** |
| `completion` (`Completion`) | outcome, reason | enumerated final state, never an inferred claim about model answers |
| `diagnostic` (`Diagnostic`) | name, metrics | **opt-in only**, fixed numeric allowlist: latency, counts, channels, retries |

The codec may be used without databases, models, GPU, microphones, or external dependencies. `decode_contract` dispatches by validated `kind`; `Class.from_json` additionally checks the expected type.

## Persisted schema

The existing `Store` SQLite database is reused in WAL mode, using `Store.lock` and a five-second busy timeout. New namespaced objects:

- `cognitive_sessions(id PRIMARY KEY, started_at, last_active_at)`
- `cognitive_tasks(id PRIMARY KEY, session_id, source, started_at, state)`
- `cognitive_events(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id, task_id, at, kind, contract_json)`
- Indexes `idx_cognitive_events_task`, `idx_cognitive_tasks_session`
- Trigger `cognitive_events_immutable` rejects modification of historical event rows.

For a command, Alfred opens/reuses one process-local session and creates exactly one task before model inference. Events append in transaction order (`id ASC`). No task may be appended after completion, and malformed stored history refuses both reading and further appends. SQLite transactions protect concurrent readers/writers across threads/processes. Event history is capped at **64 events per task**, and listing to **100 tasks per request**. `SessionLedger.prune(before_epoch)` explicitly removes *finished, inactive* sessions; running tasks are not purged. No automatic retention/deletion occurs during normal commands.

**Sample trace (structural, hypothetical):**

```text
task{session_id:uuid,task_id:uuid,source:text,input_chars:18}
decision{engine:needle3,route:direct,call_count:1}
observation{tool:get_gpu_status,outcome:returned}
completion{outcome:completed,reason:executed}
```

Wake-gated voice commands carry `source:voice`, one-shot `--text` commands carry `source:text`, and the local `--reason` entry has `source:reason`. Unauthorized wake-word/bystander speech is not assigned a task. A wake-authorized shutdown uses `decision(local,shutdown)` without invoking Needle/Qwen. The existing deterministic GPU status correction is retained; simple direct status commands do not add Qwen inference.

## Privacy boundary

**Default: no transcript, raw audio, microphone samples, prompts, user messages, tool arguments, or tool results in cognitive tables.** Even if a model or tool emits a credential, the contracts cannot serialize that value, because these fields do not exist. Sensitive actions are still governed by the preexisting local `System2` approvals.

The optional configuration `runtime.cognitive_diagnostics: true` permits bounded **numeric-only** Needle route timing and input-length counters. Unrecognized strings, arbitrary diagnostic keys, transcripts, API keys, passwords, email addresses, and model text are rejected rather than heuristically scrubbed. Leave the flag false in normal use (the default). Existing **legacy** tables retain their historical behavior, including reminder messages and Phase 2/4 approval/workflow data; this migration never purges or retroactively redacts legacy data.

`MemorySessionLedger` supplies an in-memory SQLite-backed fake adapter for isolated tests and clients.

## Migration, downgrade and rollback

Migration on `SessionLedger(Store(path))` is additive/idempotent (`CREATE TABLE IF NOT EXISTS`, index/trigger creation). It does not modify `events`, `reminders`, `workflows`, `approvals`, `workflow_runs` or other earlier tables. It does not take over SQLite `user_version`. No DeepSeek or additional package dependency is required.

1. Stop Alfred and take a consistent SQLite backup using the Python `sqlite3.Connection.backup` API (recommended in WAL mode), not a copy of just the live `.db` file.
2. Apply the branch/start Alfred; old tables remain queryable. Verify `python -m unittest discover -s tests -v` on Python 3.10 and 3.12.
3. To roll back, stop Alfred, restore a backup, or use the previous version of Alfred with the extra namespaced tables left intact. The previous version ignores these new tables.
4. Only if discarding **all** new task history is explicitly desired, after backup and with Alfred stopped, drop `cognitive_events`, `cognitive_tasks`, `cognitive_sessions` plus their associated trigger/indexes. Never drop existing `events` or approvals. Old Alfred does not require destructive rollback.

## Offline validation

```sh
python -m compileall -q alfred tests
python -m unittest discover -s tests -v
```

`tests/test_cognitive.py` covers strict round-trips/version/schema, duplicate keys, oversize payloads, private-field rejection, corrupt event rejection, event ordering and immutability, restart/read-after-reopen, migration idempotency, selective retention, concurrent WAL clients, the in-memory fake, no persisted secrets, text/voice correlation and no redundant Qwen call for a simple status command.

Hardware-specific voice capture, real-time microphone clocks, native Needle/Whistle execution, and live Jetson resource consumption are **not** asserted by these offline tests.
