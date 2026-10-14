# Issue #15: versioned Needle → Qwen same-task continuation

**Status:** opt-in, Python-tested local integration. Native Needle 3 on the
physical 8 GB Jetson remains gated by [#29](https://github.com/Siddharthgolecha/Alfred/issues/29).
Independent review/merge is required before deployment. This is **not** a
general autonomous tool agent.

## Activation and rollback

The new `runtime.escalation_enabled` defaults to `false`; `needle_loop_enabled`
also defaults to `false`. To test only the handoff without native multi-turn
Needle feedback, set `"escalation_enabled": true` in the existing `runtime`
config and restart Alfred. Turn it off to immediately restore #14 / legacy
Needle→Qwen behavior.

```json
{
  "runtime": {
    "escalation_enabled": true,
    "needle_loop_enabled": false
  }
}
```

Do not copy a minimal config directly over your normal config (Alfred merges
it with defaults; retain your local model URL/other settings). Qwen continues
to use the existing `qwen.url` and configured backend (llama.cpp, Ollama,
LM Studio or llama-server); the SDK is optional and does not start a separate
model service. Disabled/unavailable Qwen produces an explicitly **incomplete**
result; never a false claim of successful processing.

## Precise ownership and policy

`Alfred.handle_text()` creates exactly one durable `TaskEnvelope` after a
trusted text CLI or wake-authorized transcript. A bounded ephemeral
`EscalationEnvelope(version=1)` carries the **original authorized request**,
canonical session/task IDs, *executed read-only tool names*, only finite/known
telemetry metrics, remaining tool and Needle decision budget, reason for
escalation and whether actions may be proposed. Completed action-bearing
steps are **never resumable or auto-replayable**.

- **Needle direct result:** regular `System2.direct` path, no Qwen or Harness.
- **Unsupported / ordinary explanation:** optional read-only Qwen continuation.
- **Low Needle confidence:** optional read-only Qwen continuation, never convert
  an uncertain action into an authorized action.
- **Explicit Needle `delegate_to_reasoner`:** Qwen may *propose* tool actions
  within existing concrete allowlists. Sensitive steps remain **pending** until
  a separate trusted `alfred --approve ID`; unchanged Phase 4 graph workflow
  digest protections still apply. `approve` is never present in model schemas.
- **Needle loop budget/repeated/uncertain after successful reads:** pass only
  whitelisted bounded values from already completed System2 reads to read-only
  Qwen. It may propose new, distinct reads if remaining budget permits; it
  cannot repeat the same tool+args or propose a side effect.
- **Unsafe/suppressed tool, cancellation, wake authorization failure:** stop and
  clarify. Do not start another model automatically. The already completed
  read-only prefix remains visible with an explicit halt reason.
- **Qwen or Harness failure:** no follow-up tools run, response contains
  `status: "incomplete"`; already completed observations are retained.
  A fresh direct Qwen inference may replace a crashed SDK *only before tool
  execution begins*. Neither System2 execution nor approvals are inside a
  retry/fallback handler.
- **Invalid/forged plan or attempted replay:** hard rejection before staging
  or running tools. No second attempt to reinterpret it through Qwen.

Only the protected Python `System2.parse_plan → EscalationEnvelope.reconcile →
System2.execute_or_stage` pipeline can execute/stage a Qwen proposal.
`allow_actions=false` rejects reminders, service control, arbitrary tasks,
workflow saves, and other mutations. The precise service/task allowlists and
approval decisions are still enforced by existing `System2.preflight` and
`System2.approve`, not by a prompt.

The active `NeedleAgentLoop` task lock still serializes native `reset → complete
→ feedback`, so another authorized task cannot mix with this one. The session
ledger records only structural decisions, handoff reason, tool names, and
completed/pending/failure state. The ephemeral envelope's source request and
observation values are **not** written to that cognitive ledger. Note that
the older, separate `Store.event("tool",...)` system stores raw tool results and
is not migrated by issue #15; do not mistake the cognitive-ledger privacy
guarantee for a global no-logging claim.

## Restart and uncertain side effects

After an application restart, the model conversation and private transient
handoff are **not** restored. A local operator can reuse the *correlation*
session using a validated existing hex ID:

```sh
alfred --resume-session <32-hex-session-id> --text "Check GPU temperature"
```

The existing SQLite cognitive session is reused, but any interrupted `running`
task is marked `failed/shutdown` and its actions are **not replayed**.
Existing one-shot pending approvals remain in SQLite and can be reviewed with
`alfred --pending`, then approved only from the trusted local CLI. A running
or partially executed task is not implicitly retried, even if its status is
indeterminate. Unknown/invalid session IDs fail closed. This preserves
correlation across restarts, **not** the last transcript or native model state.

## Local reproducible verification

```sh
python -m unittest discover -s tests -v
PYTHONPATH=. python tests/bench_needle_fastpath.py
PYTHONPATH=. python tests/bench_handoff.py
```

The new tests cover version/id/size validation, bounded prompt-injection
resistance, matching executed tool signatures, no duplicate named tasks and
services, zero elevated permissions on read-only handoff, single pending
approval through the existing SQLite queue, one-shot execution, Qwen
unavailability, SDK→Qwen pre-execution fallback, no fallback after a partial
side effect, same task/session ledger correlation, interrupted session
recovery, and a **real local HTTP /v1/chat/completions protocol smoke** using a
deterministic stub server. That protocol smoke uses a stub instead of live
Qwen weights; the separate CI `harness-sdk-smoke` job executes the pinned
genuine SDK/Cordis process, but native Jetson/Needle weights are still untested.

The paired `bench_handoff.py` compares clear direct status routing against
Qwen-handoff routing on the **same CI host** with interleaved, deterministic
fake model responses. It prints P50/P95 and Qwen/direct invocation counts.
This measures Python policy/ledger serialization overhead only; **it does not
include actual Qwen inference, device throughput, power, RAM or audio capture.**
Do not claim real-world performance improvement based on this benchmark.

## Remaining rollout blocker

[#29](https://github.com/Siddharthgolecha/Alfred/issues/29) must demonstrate
native Needle manual `complete(JSON result)` compatibility on the actual Jetson,
session/reset correctness, true device P50/P95 under load, and power/thermal/
RAM headroom. Until qualified, both new multi-step continuation features
remain default-off.
