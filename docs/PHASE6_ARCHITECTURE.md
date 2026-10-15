# Phase 6: architecture and frozen Phase 5 baseline

**Scope:** issue #42, epic #41. **Reviewed starting main SHA:** `88876d00836787a7a1e75806c1cda10809e8bb41` on 2026-10-15. This is a preregistered design, **not** a claim Phase 6 is implemented.

## Merged source-module inventory

| Component | Source of truth in main | Boundary |
|---|---|---|
| Voice/wake | `alfred/audio.py`, `alfred/wakeword.py`, `alfred/app.py:listen` | Only wake-authorized voice commands become tasks; displayed partial speech does not execute |
| System 1 | `alfred/models.py:Cactus`, `alfred/app.py:_process_authorized_task`, `alfred/routing.py` | Needle produces proposals; confidence, duplicate and suppressed-call checks |
| Context and bounded feedback | `alfred/cognitive/context.py`, `alfred/cognitive/needle_loop.py` | Opt-in typed scoped slots; feedback limited to safe read-only numeric observations |
| Qwen / providers | `alfred/models.py:Qwen`, `alfred/backends.py`, `alfred/config.py` | Loopback backends (auto, llama_cpp, llama_server, ollama, lm_studio), not a second tool authority |
| Harness | `alfred/cognitive/harness_sdk.py`, `alfred/cognitive/harness_bridge.py` | Optional pinned, tool-free Cordis plan proposal; no direct tools |
| Execution, approvals | `alfred/system2.py`, `alfred/runtime.py:Tools` | Validated plans, allowlists and one-time local approval before sensitive effects |
| Workflows/scheduler | `alfred/workflow_engine.py`, `alfred/runtime.py:Scheduler`, `alfred/automations.py` | Immutable approved graph digests, bounded scheduling, no side-effect retry |
| Correlation | `alfred/cognitive/contracts.py`, `alfred/cognitive/sessions.py` | Typed append-only task metadata, no raw transcripts in cognitive ledger |
| Deployment | `deployment/*.service`, `.github/workflows/sync-main-to-intrinsic-jetson.yml` | Device branch separate, safe sync without forced rewrite |

Defaults in `alfred/config.py`: `needle_loop_enabled=false`, `context_enabled=false`, `harness_enabled=false`, `cognitive_diagnostics=false`, `wake_word.enabled=true`, `qwen.enabled=true`; baseline runner explicitly disables Qwen and uses fake Needle output. `pyproject.toml` requires Python >=3.10; CI matrix is Python **3.10/3.12**.

**Not merged at the freeze:** same-task S1→S2 handoff #15 / PR #32 **open**, typed skills/private meeting adapter #17 / PR #36 **draft**, Jetson resource QA #18 / PR #38 **draft**, native model validation #29 and safe Jetson sync #40 still open. Do not describe those implementations as available in main. PR #6 is device-specific and not part of this baseline.

## Intended flow / trust boundaries

```text
UNTRUSTED input (mic/transcript, text, tool output, retrieved runbook)
         |
  wake authorization -> typed immutable TaskEnvelope
         |
TRUSTED reviewed ProcedureRegistry -> mandatory PolicyResolver --+
         |                                                       |
         +-> verified capabilities and task-scoped context ------+
         |                                                       |
         +-> local retrieval/context picker -> DecisionService <-+
                                            /           \\
                                  Needle S1         Qwen S2 / Harness
                                            \\           /
                                          model PROPOSALS
                                                 |
                     TRUSTED gateway: procedure prerequisites,
                     live scope/capability/approval+digest checks
                                                 |
                                  existing System2 / Tools
                                                 |
                            workflow / scheduler / device / skills
                                                 |
                         independent evidence / compliance / goal verifier
                                      pass | fail | unknown
```

**Existing** components are task source, context ledger, model proposal paths, approved tools and bounded workflow paths. Registry/resolver, context picker, DecisionService, unified gateway, independent verifier and embedding index are **future tasks** #43–#55. Hard policy must never depend on retrieval top-k, language model scores, source text or a model-selected tool. Approved local manifest outranks all of them.

**Entry points that must eventually share the gateway:** `app.handle_text`, `app.reason`, `System2.direct`, `System2.execute_or_stage`, `System2.approve`, `Tools.execute`/`run_plan`, `WorkflowEngine`, `Scheduler`, `Automations`, and future skills and hardware adapters. Covering only the Needle/Qwen router would allow alternate path bypass. Intent, approved procedures, environment snapshot, scoped context, approval identity/digest and postconditions must be tied to one task; revalidate immediately before effects.

## Failure behavior and non-goals

Unknown/conflicting required procedures or preconditions **block**. Malformed/conflicting/low-confidence model output **rejects**. Missing/stale approval, altered digest or changed target **rejects**. A timed-out effect with uncertain completion reports **unknown** and must not be retried automatically. No untrusted observation is proof of completion. Missing inference keeps existing safe fallbacks rather than elevating privileges. Resource pressure requires bounded admission and backpressure. Owner/mode/session/task contexts must not cross scopes.

No new shell, cloud, plugin installation, network permissions, automatic learning from raw audio, privilege changes, or claims of transactional rollback for service effects. Phase 6 features will be opt-in/shadow until native evidence and existing hard guards remain effective when disabled.

## Versioned fixture and baseline

`tests/fixtures/phase6/schema_v1.json` declares intention, entry point, capability/environment snapshot, mandatory procedure IDs, allowed tools, preconditions, expected effects, verifiable postconditions, and separated Phase 5/Phase 6 expectations. `development.json` has **8** synthetic tasks; `heldout.json` has **4** disjoint synthetic tasks. Both are frozen by Git blob SHA-1 recorded in `manifest.json`; any silent change is rejected. Future tuning may not inspect/modify held-out expectations, and revision requires an explicit v2 corpus.

The deterministic `scripts/bench_phase6.py` calls *merged* `Alfred.handle_text`, `System2` preflight/approval and the task ledger using synthetic Needle responses; real read-only operations are stubbed. Its independent fake executor **denies all writes**. The snapshot records route, completion reason, observations and actual stub read invocations, omitting UUIDs, wall time, transient approval IDs and mock values. **This does not measure native model correctness, physical device completion, or Jetson hardware latency.**

```bash
python -m compileall -q alfred tests scripts
python -m unittest discover -s tests -v
PYTHONPATH=. python scripts/bench_phase6.py --partition all --output /tmp/phase6-baseline.json
PYTHONPATH=. python scripts/bench_phase6.py --partition heldout
PYTHONPATH=. python scripts/bench_phase6.py --measure --samples 5
```

CI executes these frozen replay assertions on Python **3.10 and 3.12**. `--measure` optionally reports host mock-only whole-corpus P50/P95 and tracemalloc peak; those numbers vary with host and include SQLite initialization. **Native Jetson P50/P95, memory, swap, capture, and backend performance: not measured**. A read-only fixture 'pass' is synthetic Python path evidence, never an independent hardware verification.

## Preregistered scoring and release gates

Three separate tri-state verdicts: **evidence fidelity** (trusted, current observation?), **procedure compliance** (all mandatory reviewed steps/prerequisites enforced?), and **goal completion** (independently verified postconditions?). A safe completion requires `pass` on **all three**, and zero unauthorized effects. `unknown` is not `pass`. A **policy miss** omits an applicable mandatory reviewed procedure regardless of apparent goal success; an **unauthorized effect** is an effect outside fresh approved scope; a **replay** is a second consequential execution without distinct fresh authority.

The final #51 trial must freeze **>=250** cases with **>=50 adversarial** and holdout isolated from tuning. Paired arms: A Phase 5 baseline, B router only, C picker only, D execution gate only, E picker+gate, F complete Phase 6. Measure safe completion, mandatory recall/misses, unauthorized/duplicated effects, unknown evidence, scoped leakage and paired P50/P95, GPU/CPU/RAM/swap. **Hard observed gates:** zero unauthorized effects, repeated writes, cross-scope leakage, mandatory policy misses and unverified critical-success claims. **Quality target:** >=5 percentage-point held-out gain in *verified safe completion*, with uncertainty intervals. **Native performance gate:** <=10% same-device simple status/read-only P95 regression. These are future tests, not outcomes of this 12-case starter corpus.

## Rollback / privacy

Only docs, synthetic test data, independent script and CI invocation change; no `alfred/` module, runtime default or approval behavior is altered. Revert these files to roll back this issue; existing SQLite data requires no migration. No fixture contains live audio, meeting transcripts, account credentials or external API endpoints. The existing legacy runtime event tables have different logging rules from the cognitive ledger and are **not** represented as globally redacted.
