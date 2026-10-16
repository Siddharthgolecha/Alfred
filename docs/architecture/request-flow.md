# Request flow

This page follows one request from sound to side effect. Every claim is traced to
[`listener.py`](../../alfred/listener.py), [`router.py`](../../alfred/router.py) and
[`system2.py`](../../alfred/system2.py).

## 1. Voice command, happy path

```{mermaid}
sequenceDiagram
    autonumber
    actor U as User
    participant A as AudioManager
    participant S as UtteranceStream
    participant L as WakeWordListener
    participant C as Cactus (Whistle, Needle 3)
    participant G as WakeWordGate
    participant R as RequestRouter
    participant P as System2
    participant T as Tools
    participant D as Store (SQLite)

    U->>A: "Alfred, show GPU status"
    A->>S: 16 kHz mono PCM frames
    S->>L: utterance chunks (RMS voice activity)
    L->>C: transcribe_stream(chunks)
    Note over L: admission.set_capture_active(true) during STT
    C-->>L: transcript
    L->>G: consume(transcript)
    G-->>L: "show GPU status" (wake prefix stripped)
    L->>R: handle_text(command) with source="voice"
    R->>D: start task, append TaskEnvelope
    R->>C: route(text)
    C-->>R: function_calls, confidence
    R->>R: validate shape, confidence, tool budget
    R->>P: direct(calls)
    P->>P: preflight (schema + allowlists)
    P->>T: run_plan(steps) [read-only: no approval needed]
    T->>D: event("tool", ...)
    T-->>P: results
    P-->>R: results + verified reads
    R->>D: Observation, Completion
    R-->>U: Feedback (console, optional spd-say)
```

Key properties:

- The **wake gate** runs *after* transcription in the default `transcript` mode. Unaddressed
  speech is transcribed but discarded and never reaches `RequestRouter`. In `openwakeword` mode an
  ONNX detector gates Whistle itself. See [Wake-word guide](../guides/wake-word.md).
- A **standalone wake** ("Alfred") returns `""`. The gate arms for `wake_word.followup_seconds`
  (default 8) and the next utterance is accepted once.
- A **spoken shutdown** (`close`, `exit`, `quit`, `stop listening`, `shut down`, `shutdown`) is
  matched by a regular expression in the listener *before* the router. It records a `shutdown`
  decision and completion and stops the loop. No model is asked.
- `RequestRouter.handle_text` holds `_needle_execution_lock`, so commands are processed one at a
  time.

## 2. Router decision tree

After `Cactus.route()` returns, `_process_authorized_task` applies these checks in order.

```{mermaid}
flowchart TD
    start([Authorized text]) --> clear{"'clear this session'?"}
    clear -- yes --> cleared["Clear session slots and observations.<br/>No model call."]
    clear -- no --> resolve["Resolve pronoun references<br/>(ContextManager or SessionSlots)"]
    resolve --> amb{"Ambiguous or<br/>action pronoun?"}
    amb -- yes --> rej1["Rejected: unsafe_route"]
    amb -- no --> route["Cactus.route(text)"]
    route --> shape{"Calls well-formed?"}
    shape -- no --> rej2["Rejected: unsafe_route"]
    shape -- yes --> dup{"Duplicate calls<br/>(loop enabled)?"}
    dup -- yes --> rej3["Rejected: unsafe_route"]
    dup -- no --> supp{"suppressed_calls or<br/>ungrounded arguments?"}
    supp -- yes --> rej4["Rejected: unsafe_route"]
    supp -- no --> conf{"confidence in<br/>[floor, 1]?"}
    conf -- no --> unc{"escalation_enabled?"}
    unc -- yes --> h1["Handoff to Qwen<br/>reason=uncertain, read-only"]
    unc -- no --> rej5["Rejected: low_confidence"]
    conf -- yes --> known{"Any tool name not<br/>in the registry?"}
    known -- yes --> h2["Handoff to Qwen<br/>reason=unsupported, read-only"]
    known -- no --> budget{"More calls than<br/>needle_max_tools?"}
    budget -- yes --> rej6["Rejected: unsafe_route"]
    budget -- no --> kind{"Which route?"}
    kind -- "no calls" --> h3["Handoff to Qwen<br/>reason=unsupported, read-only"]
    kind -- "single delegate_to_reasoner" --> h4["Handoff to Qwen<br/>reason=delegated, actions allowed<br/>(staged for approval)"]
    kind -- "direct, multi-step wording" --> nl["NeedleAgentLoop"]
    kind -- direct --> d["System2.direct(calls)"]
    nl --> d2{"Loop stop reason"}
    d2 -- "uncertain, repeated,<br/>partial, budget" --> h5["Handoff reason=incomplete<br/>with completed reads"]
    d2 -- "unsafe, cancelled" --> halt["Halt: incomplete"]
    d2 -- "completed, protected" --> done
    d --> pol{"Policy outcome"}
    pol -- "sensitive tool" --> stage["Stage approval record"]
    pol -- "read-only or reminder" --> run["Execute"]
    stage --> done([Completion + Feedback])
    run --> done
    h1 --> done
    h2 --> done
    h3 --> done
    h4 --> done
    h5 --> done
```

The checks guard against a misbehaving model, not just a mistaken one. A request that fails any
of them never reaches `Tools`.

## 3. Needle to Qwen handoff

When Needle cannot handle a request, the router builds an `EscalationEnvelope`
([`cognitive/escalation.py`](../../alfred/cognitive/escalation.py)) and calls
`System2.continue_handoff`. The **same** `TaskEnvelope` continues, so the task ledger stays
correlated. The user is not asked to repeat anything.

```{mermaid}
sequenceDiagram
    autonumber
    participant R as RequestRouter
    participant E as build_handoff
    participant P as System2
    participant H as LocalHarness
    participant Q as Qwen (via Cordis)
    participant D as SessionLedger

    R->>E: session_id, task_id, request, reason,<br/>completed calls + results
    E->>E: validate reads only, reduce results to metrics,<br/>set tool and decision budget
    E-->>R: EscalationEnvelope (ephemeral)
    R->>D: Escalation(needle3 -> qwen), Decision(qwen)
    R->>P: continue_handoff(envelope, HandoffConfig)
    P->>P: admission.claim("qwen"); supervisor.ensure_running()
    P->>H: plan(request, tools=catalog, handoff=envelope)
    H->>Q: JSON-only planning prompt
    Q-->>H: {message, steps, save_as}
    H-->>P: untrusted plan
    P->>P: parse_plan -> preflight -> envelope.reconcile(steps)
    Note over P: Rejects replayed tools, unknown tools,<br/>over-budget plans, and writes in a read-only handoff
    P->>P: execute_or_stage(steps, allow_actions=envelope.allow_actions)
    P-->>R: result (executed, staged approval, or incomplete)
    R->>D: Observation(s), ApprovalReference?, Completion
```

What the envelope carries to Qwen, and what it does not:

| Sent | Never sent |
|---|---|
| The original request (at most 1200 characters) | Raw tool output, stdout, service names |
| Handoff reason, `allowed_actions`, remaining tool and decision budget | Tool arguments of completed calls |
| Names of completed tools | Audio or earlier transcripts |
| Bounded numeric metrics (`gpu_temp_c`, `service_count`, …) | Any free-text from a tool |

The catalog offered to Qwen excludes `delegate_to_reasoner` and `run_workflow`. For a read-only
handoff it is further limited to the read tools.

### Failure behaviour

| Failure | Result |
|---|---|
| Qwen or Harness unavailable, timeout, connection error | `status: incomplete`, completed reads preserved, no fallback planner |
| Malformed plan (`HarnessInvalidPlan`, `QwenPlanResponseError`) | `invalid_plan`, nothing executed or scheduled |
| Plan fails `parse_plan` or `reconcile` | `invalid_plan`, previous telemetry preserved |
| Qwen disabled in config | `incomplete`, "Qwen is disabled" |

Validation and execution sit outside the exception handler on purpose. Once execution starts, a
failure is never treated as a signal to re-plan, because tools cannot be safely replayed.

## 4. Approval

`System2.needs_approval` is true when the plan has a `save_as` name or contains any tool outside
`VOICE_EXECUTABLE_TOOLS`.

| Executes immediately by voice | Staged for local approval |
|---|---|
| `get_system_status`, `get_gpu_status`, `list_services`, `list_reminders`, `list_schedules`, `list_workflows`, `list_automations`, `create_reminder`, `create_reminder_at` | `control_service`, `run_named_task`, `run_workflow`, `cancel_reminder`, `schedule_every`, `cancel_schedule`, `create_daily_reminder`, `watch_gpu_temperature`, `pause_automation`, `resume_automation`, `cancel_automation`, and any plan that saves a named workflow |

```{mermaid}
stateDiagram-v2
    [*] --> pending: Approvals.propose()
    pending --> running: Approvals.claim() (CLI --approve, atomic UPDATE)
    pending --> rejected: Approvals.reject() (CLI --reject)
    pending --> expired: expires_at passes
    running --> complete: finish(result)
    running --> failed: finish(error)
    complete --> [*]
    failed --> [*]
    rejected --> [*]
    expired --> [*]
```

- `claim` is a single transactional `UPDATE ... WHERE status='pending' AND expires_at>?`, so an
  approval can run at most once.
- `approve` re-runs `preflight` on the stored steps before executing them.
- A record left in `running` (for example after a crash) is **never** replayed.
- The default lifetime is `runtime.approval_ttl_seconds` (180 s).
- Graph workflows add a digest check: if the stored workflow changed after the voice request,
  approval fails with "Workflow changed since voice approval request".

`expired` is not a stored status. `pending()` and `claim()` simply ignore rows whose
`expires_at` has passed.

## 5. Text and reasoning entry points

| Entry | Path |
|---|---|
| `--text "…"` | `Alfred.handle_text` → the same `RequestRouter` path, `source="text"`, no wake phrase |
| `--reason "…"` | `Alfred.reason` → `LocalHarness.plan` → `parse_plan` → `System2.execute_or_stage`, `source="reason"`; exit code 1 if the plan is `incomplete` |
| Scheduled job | `Scheduler` → `Tools.execute` directly; read-only tools only |

Next: [Cognitive layer](cognitive.md).
