# Phase 5 / issue #14 — bounded contextual Needle 3 loop

**PR #28:** prerequisite #13 / PR #27 has now merged into `main`. PR #28 must incorporate the **reviewed main** code, retarget `main`, and receive independent code and native-device review. Do not sync `intrinsic_jetson` from Worker A.

## Architecture and authority

This uses upstream Cactus Needle 3's documented manual feedback API:
- `agent.complete(user_text)` picks a bounded tool call; the existing `Cactus.route()` calls `agent.reset()` **once at each new authorized task**.
- Alfred validates every call through existing `System2` preflight and executes only via `System2.direct()`. **Never call `agent.run()`**, because it automatically executes Python handlers outside Alfred's trusted policy.
- Only when explicit multi-step intent appears, and a **safe read-only** tool returned, `NeedleAgentLoop` sends a strict scalar-only `json.dumps({"observations":[...]})` to `Cactus.route_feedback()` → `agent.complete(json_string)` **without reset**. Native Needle decides whether to call a second tool or respond.
- All System2 approvals, special graph workflow digest approval, concrete allowlists, cancellation/side-effect protections and Qwen delegation are unchanged. There is **no tool execution under Cordis/DeepSeek**; a single logical `session_id` remains available for independent Harness SDK/Qwen planning.

An Alfred-wide `_needle_task_lock` serializes `handle_text` across the entire `reset → route → validated System2 tools → route_feedback → finalize` sequence, preventing concurrent authorized tasks from corrupting the native Needle agent's context. Shutdown can signal `_needle_cancel` without acquiring that lock; cancellation is checked before every subsequent decision/tool/feedback boundary. There is **no long-lived shared native Needle conversation** across user requests. Native history is reset on each authorized command; only a tiny, verified session slot crosses command boundaries. New user utterances, including pronoun follow-ups, are not silently appended to the last native conversation.

## Bounds and fail-closed handling

`runtime.needle_loop_enabled` defaults to **false** until native Jetson acceptance #29, with `needle_max_decisions:3`, `needle_max_tools:3` (each can be lowered to 1) and `needle_context_ttl_seconds:120` (1..300).

- One-shot path: ordinary `get_gpu_status` / `get_system_status` uses exactly **one model decision, one System2 execution, zero feedback turns and zero Qwen calls**. The multi-step path activates only for explicit temporal/conditional markers such as **then / afterwards / after that / next / depending on / based on**.
- Each continuation rechecks confidence (strict finite non-boolean number in [`needle_confidence_floor`, 1]), ungrounded/suppressed calls, complete tool schema and allowlist, total decision/tool budgets, duplicate call fingerprints, cancellation and authorization. A repeated equivalent tool call is **not executed twice**.
- Only `get_gpu_status`, `get_system_status` and `list_services` can feed back into Needle: the two status calls yield whitelisted numeric/boolean metrics; `list_services` yields **only** a bounded numeric `service_count` (no names, statuses or raw text). Other read-only listings execute via the existing System2 one-shot path without opaque feedback. Consequential commands can still appear on an initial turn, but are immediately handled/staged under `System2.direct` and **never reused as feedback**. Consequential proposals after feedback are rejected. No nested delegation, model-generated shell execution, or automatic action approval is possible.
- Feedback permits only finite, bounded **numeric/boolean** keys `available`, `gpu_temp_c`, `uptime_seconds`, `root_free_gib`, and `service_count` for the service-list aggregation. All strings (even apparently harmless text), dict/list payloads, `stdout`, raw `tegrastats`, adversarial tool results, files and secrets are **dropped**. Observations in the append-only cognitive ledger remain metadata-only.
- Stop safely on ambiguity, ungrounded/suppressed/low-confidence call, duplicate, corrupted argument, budget limit or cancellation. After any validated read-only results have completed, **any** subsequent stop (budget, repeated call, unsafe proposal, low confidence, or cancellation) preserves those actual observations and returns `partial: true` with the explicit stop reason and notice that additional requested steps remain unverified; the ledger records the executed prefix truthfully. Before any execution, stop without inventing results. Already-completed read-only observations remain recorded; no automatic retries or second attempts at side effects.

**Wake authorization:** only commands returned by `WakeWordGate.consume()` enter the loop. The wake gate consumes one command and disarms; a running authorized command is not a perpetual microphone session. `Alfred.close()` sets `_needle_cancel` and terminates the active loop at its next decision/feedback boundary. There is no safe way to preempt an already-active synchronous native `complete()` call without native cancellability; physical-device cancellation timing is part of #29.

## Trusted contextual references

The only supported pronoun slots are fresh **GPU-temperature** references, scoped by the existing `session_id`:
1. An **explicit** user request naming GPU and temperature must succeed through `get_gpu_status` and return a valid, finite GPU reading (`available=True`, -50..150 °C).
2. Only then, for at most `needle_context_ttl_seconds`, `"check it again"`, `"show that"` etc. may be canonicalized to `"Show GPU temperature again"` **before** a new Needle task. It does not recover any tool arguments from raw model output.
3. Expired, absent, cross-session, denied/ambiguous, unrelated or changed-topic slots fail closed and ask for the metric explicitly. An unrelated intervening command clears the prior slot.
4. Nothing from a microphone transcript, arbitrary tool string, Harness proposal or prior Session memory is used to create a reference. These ephemeral slots contain only `("gpu_temperature", monotonic_timestamp)` and are cleared at Alfred shutdown.

The session ledger still records exactly one task for each authorized utterance, including rejected pronouns.

## Operational examples

```text
Alfred, check GPU temperature
  Needle #1 -> get_gpu_status -> System2.direct -> output
  No feedback turn, no Qwen call

Alfred, check it again
  SessionSlots -> "Show GPU temperature again" -> fresh Needle #1
  If TTL expired or topic changed: clarify, no tool

Alfred, check GPU temperature then check system status
  Needle #1 -> get_gpu_status -> System2.direct -> filtered JSON metrics
  Needle #2 (same native conversation) -> get_system_status -> System2.direct
  Needle #3 -> respond (zero tool calls), stop
```

## Evidence and performance

Portable tests: `python -m unittest discover -s tests -v` on Python 3.10/3.12; specific module `tests/test_needle_loop.py` covers safe two-step feedback, session isolation, context freshness, tool-result prompt injection, invalid/malicious tool calls, duplicate side effects, budget exhaustion, cancellation, native API `Needle` fake, and Alfred voice/routing contract compatibility.

**Repeatable P50/P95 fast path measurement:** `python tests/bench_needle_fastpath.py`. Same host, interleaved 300 runs per condition, deterministic mock Needle/Tools; compare `needle_loop_enabled=false` against `true` with `GPU status`. Bench output provides microseconds for both. **This measures only Python policy/database overhead; there is no actual inference/hardware timing and no speedup claim.** Jetson GPU power, RAM, P50/P95 and native Needle API acceptance must be recorded separately on the physical 8GB Jetson.

Native hardware tracking: [Jetson validation blocker #29](https://github.com/Siddharthgolecha/Alfred/issues/29). The published `cactus-needle` API confirms manual `complete(json.dumps(result))` and `reset()`; the Python CI shim does **not** prove binary compatibility with deployed Jetson, model weights or audio capture.

## Rollback

The default is already off. After explicitly opting in with `"runtime": {"needle_loop_enabled": true}`, roll back using `"runtime": {"needle_loop_enabled": false}` and restart Alfred. All other Phase 2-5 code remains available; no schema change or migration is necessary. The `needle_max_decisions=1` setting also removes all potential feedback inference, but leave the flag disabled for an exact single-step rollback. No new permissions are granted by these settings.
