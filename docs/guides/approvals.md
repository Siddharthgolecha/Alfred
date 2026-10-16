# Approvals

Alfred never lets a spoken command perform a sensitive action by itself. Sensitive plans are
**staged** in SQLite, and you approve or reject them from a terminal on the device.

Why it works this way: [ADR 0003](../architecture/decisions/0003-local-approval-only.md) and
the [security model](../architecture/security-model.md).

## What needs approval

Any plan that contains a tool outside the direct set needs approval. The full tier of each tool
is in the [tool reference](../reference/tools.md#execution-tiers). In short:

| Runs immediately | Needs `--approve` |
|---|---|
| Status and list tools, one-shot reminders | Starting or stopping a service, running a named task, daily reminders, GPU watches, pausing or cancelling anything, recurring schedules, running a saved workflow, and saving a plan as a workflow |

When Needle returns nothing it can act on, Qwen may still answer in **read-only mode**. If Qwen
then suggests a mutating action, Alfred refuses it. It does not drop the step and claim success.

## The approval loop

```{mermaid}
sequenceDiagram
    actor U as You
    participant A as Alfred (voice or --reason)
    participant DB as SQLite approvals
    participant C as Terminal (alfred CLI)

    U->>A: "Alfred, stop the scheduler service"
    A->>DB: propose steps (status = pending, expires in TTL)
    A-->>U: "Awaiting local approval" (approval id 7)
    U->>C: alfred --pending
    C->>DB: list unexpired pending
    U->>C: alfred --approve 7
    C->>DB: claim (pending to running)
    C->>C: re-validate every step
    C->>DB: finish (complete or failed)
```

1. **Propose.** A voice command or `--reason` produces a plan. The sensitive parts are stored as
   a pending approval with an expiry.
2. **Review.** List what is waiting:

   ```bash
   alfred --pending
   ```

3. **Decide.** Run it once, or discard it:

   ```bash
   alfred --approve 7
   alfred --reject 7
   ```

`--pending`, `--approve` and `--reject` load no speech models and print JSON. `--reject` prints
`{"rejected": true}` if a pending approval was rejected and `false` otherwise.

## Rules

- **Single use.** `--approve` claims the record atomically (`pending` to `running`). A second
  attempt fails with "Approval missing, expired, or already acted on".
- **Expiry.** A pending approval lives for `runtime.approval_ttl_seconds` (default 180). Expiry
  is enforced when you approve. `--pending` hides expired items.
- **Re-validation.** Approval is not a rubber stamp. Every step is checked again against the
  tool schemas and the service and task allowlists before anything runs.
- **Voice cannot approve.** The approval commands are not in the model's tool schema and no
  voice phrase maps to them. Confirming from the headset is never treated as approval.

### Approving a saved graph workflow

When a voice request runs a graph workflow, the staged approval records the graph's
**digest**. On `--approve`, Alfred loads the workflow again and compares digests. If you edited
the graph in between, the approval fails with "Workflow changed since voice approval request"
and nothing starts. See [Workflows](workflows.md).

## Outcomes and failure

| Status | Meaning |
|---|---|
| `pending` | Waiting for you. |
| `running` | Claimed by an `--approve`. |
| `complete` | All steps finished. The result is stored. |
| `failed` | A step raised an error. The error text is stored, truncated. |
| `rejected` | You rejected it. |

## Limits to be aware of

- Anyone who can run the `alfred` CLI as your user can approve. The control is the operating
  system account. **Never run Alfred as root.**
- A multi-step plan is validated first, but completed steps are **not rolled back** if a later
  step fails.
- If the process crashes after a claim, the approval stays `running`. It is never replayed
  automatically, to avoid duplicate side effects. Inspect the effects by hand.
- Expired approvals stay in the database. They are not deleted.
- Pending approvals hold the plan's arguments, which can include personal text such as a
  reminder message. Keep the database directory private.
- There is no cloud fallback if Qwen is unavailable.
- Parameter validation and allowlists block unknown tools. They cannot prevent every unwanted
  outcome of a task you have allowed.
