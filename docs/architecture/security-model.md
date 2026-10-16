# Security model

Alfred is designed for a single-user device in a space where recording is appropriate. It does
not defend against an attacker who can already run arbitrary code as your user: Alfred's
policy code runs in the same process. The goal is to stop **speech, model output and tool
output** from gaining authority they were not given.

## Authority boundaries

```{mermaid}
flowchart LR
    subgraph untrusted["Untrusted input"]
        speech["Ambient speech"]
        needle["Needle 3 output"]
        qwen["Qwen / Cordis output"]
        toolout["Tool output text"]
        facts["Retained facts"]
    end
    subgraph gates["Gates (Python)"]
        wake["Wake-word gate"]
        shape["Shape + confidence checks"]
        val["Schema validate()"]
        pre["System2 preflight<br/>allowlists"]
        recon["Handoff reconcile<br/>budget, no replay"]
        appr["Local CLI approval"]
    end
    subgraph trusted["Authority"]
        tools["Tools: fixed handlers"]
    end

    speech --> wake --> needle
    needle --> shape --> val --> pre
    qwen --> val
    qwen --> recon --> pre
    pre -- "read-only or reminder" --> tools
    pre -- "sensitive" --> appr --> tools
    tools -. "numbers only" .-> needle
    tools -. "numbers only" .-> qwen
    toolout -. "never fed back" .-x qwen
    facts -. "data, not instructions" .-> qwen
```

| Source | Treated as | Can it execute something? |
|---|---|---|
| Speech without the wake phrase | Discarded | No |
| Wake-authorized speech | A request | Only through the gates below |
| Needle 3 output | Untrusted proposal | Only if it passes validation, confidence and policy |
| Qwen / Cordis output | Untrusted proposal | Same, plus handoff reconciliation |
| Tool output | Data | Never fed back as text; reduced to allowlisted numbers |
| Retained facts and context | Data, not instructions | No |
| Local CLI (`--approve`) | Operator action | Yes, after re-validation |

## What the code enforces

### No arbitrary shell

- `control_service` only works for names in `runtime.allowed_services` and runs
  `systemctl --user <verb> <name>`. Names must match a strict pattern. No `sudo`.
- `run_named_task` only runs an argv **list** from `runtime.named_tasks`, with `capture_output`,
  a 60 s timeout and no shell. An entry that is not a non-empty list of strings is refused.
- Neither can appear in a plan unless it passes `preflight`, which checks these allowlists
  *before any step executes*.

### Tool validation

`runtime.validate()` rejects unknown tools, unknown or missing arguments, wrong types,
out-of-range integers, over-long strings and values outside an `enum`. `validate_plan()` also
caps the step count (`runtime.max_plan_steps`), forbids nested `delegate_to_reasoner`, forbids
`run_workflow` inside a plan, and restricts conditions to a top-level field of an *earlier*
step with a fixed operator set.

### Approval

See [Approval](request-flow.md#4-approval). In short: only a fixed list of read-only tools and
simple reminders run on a voice command; everything else is staged and needs
`alfred --approve ID` from a local terminal. Approval is one-shot, time-limited, atomic, and
re-validated at execution. Needle's tool schemas do not include `approve`, `reject` or any
administrative operation.

### No replay

An interrupted task is never re-run. A session left behind by a dead process requires an
explicit `--resume-session`, which closes running tasks as failed. Workflow steps left in
`running` after a crash need explicit `--workflow-reconcile`. A handoff refuses to resume a task
whose completed prefix contained a write.

### Fail closed

| Situation | Result |
|---|---|
| No microphone delivers PCM | Exit code 2 before models load |
| Harness missing, wrong version, or plugin graph differs | Interactive start fails |
| Inference port occupied by another service | `RuntimeError`; the service is not replaced |
| Plan malformed | Nothing executed or scheduled; reported as incomplete |
| Admission refused under memory/swap/CPU pressure | The inference request is rejected, not queued indefinitely |
| Control directory not owned by you with mode `0700` | `PermissionError` |
| Config contains an unknown key | `ValueError` |

### Local only

`qwen.url` must be `http://` + loopback host + explicit port with no path (`config.py`). The
Harness repeats that check. The backend is started bound to `127.0.0.1`. Needle telemetry is
disabled by setting `NEEDLE_TELEMETRY=0` unless `runtime.telemetry` is true. The Harness API key
is a placeholder constant.

### Control socket

`alfred close` talks to a Unix socket at `<runtime.logs>/.control/<role>.sock`. The directory
must be a real directory owned by you with mode `0700` and the socket is `0600`. Where
`SO_PEERCRED` is available, the server and client both verify the peer UID. There is no PID
file and no `pkill`. A second instance of the same role refuses to start.

## Privacy

| Data | Stored? |
|---|---|
| Raw audio | No |
| Transcript text | No. `events` records the *length* of an utterance. |
| Cognitive ledger | Structural metadata only |
| Tool arguments and results | **Yes**, in the `events` table (`Store.event("tool", ...)`, truncated to 6000 characters) |
| Approved facts | Memory only unless `context_persistence_enabled`; then SQLite with file mode `0600` |
| Harness session JSONL | In a private `0700` temp directory until `close()` |

Do not describe the database as zero-retention. The `events` table can contain tool arguments
and results, and nothing scrubs historical rows. Logical deletion of facts does not erase
backups, snapshots or WAL history. There is no encryption at rest. Use disk encryption for
sensitive deployments.

## Known limits

- The wake gate and fact filters are heuristics. They reduce, but do not eliminate, prompt
  injection and false activations.
- `InferenceAdmission` is a thread-level gate. It cannot preempt a native model call that has
  already started.
- Scheduled and workflow delivery is at-least-once: a crash between delivery and
  acknowledgement can duplicate a notification.
- Microphone presence is checked by receiving PCM bytes. That is a functional test, not proof of
  physical hardware.
- The Cordis runtime is audited as shipped. The audit proves the plugin graph matches the pinned
  inventory, not that the upstream binary is bug-free.
- Alfred must not run as root.

Reporting a vulnerability: see [SECURITY.md](https://github.com/Siddharthgolecha/Alfred/blob/main/SECURITY.md).
