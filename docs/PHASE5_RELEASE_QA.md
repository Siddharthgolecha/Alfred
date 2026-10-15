# Phase 5 issue #18 — 8 GB Orin admission and release QA

**Status: offline implementation, NOT release-qualified.**
Base: latest `main` as of 2026-10-15. Issue #18 depends on #13–#17:
#15 PR #32, #16 PR #33 and #17 PR #36 have now merged into `main`.
This branch integrated current `main` `388fdb71b87152077a9fcdf5e6536e73a174deb3`
with a non-force two-parent merge, preserving same-task handoff,
scoped context and typed skill discovery.
**Draft for physical 8GB Jetson acceptance and independent review.**
No native hardware release evidence or owner-approved thresholds exist.

## Architecture: resource admission and rollback

`runtime.resource_admission_enabled` defaults **false**. Opt-in example:

```json
{
  "runtime": {
    "resource_admission_enabled": true,
    "resource_min_available_mib": 512,
    "resource_max_swap_used_mib": 1024,
    "resource_max_cpu_load_per_core": 3.0,
    "resource_max_wait_ms": 200
  }
}
```

This is a fragment to merge into an existing Alfred configuration, NOT a full
configuration. Disabling the flag returns to the existing Needle/System2/
Harness path without changing prior tool/approval policies or session schema.

A single `InferenceAdmission` object is shared by:

- `Cactus.route` (native Needle `reset -> complete`) and `route_feedback`;
- `Cactus.transcribe_stream` (native Whistle);
- `Qwen.complete` (including model readiness and generation);
- `Qwen.explain` (read-only post-tool explanation);
- `LocalHarness.run`, which handles optional SDK planning in the same slot.
- Qwen model-supervisor watchdog startup also uses the slot when opt-in.

One Python Condition-protected inference owner is admitted at a time; queue
capacity is **one ordinary waiter plus at most two reserved Whistle
waiters** (three total), timeout 0–10,000ms, and Whistle gets
priority on the next free slot **FIFO among Whistle tickets**. A third
queued Whistle request is explicitly rejected with no hidden retries or tool
side effects. Background turns cannot wait indefinitely: each ticket has a
strict deadline and can be rejected during a sustained voice burst. During active voice ASR, the watchdog
defers Qwen cold startup and no new optional Qwen/Harness turn can be
admitted. Linux `/proc/meminfo` and load average are sampled without a
separate `tegrastats` subprocess inside the hot path. Missing/unreadable
resource readings **fail closed** in opt-in mode. Critical available-memory
or swap pressure rejects even Whistle admission; high normalized CPU load
rejects non-Whistle inference. Transcripts, tool arguments and raw model
responses never appear in admission snapshots. Instrumentation is shed when
busy or under pressure.

**Limitations:** This is cooperative scheduling, NOT kernel GPU isolation.
One already running Qwen/native SDK inference cannot be preempted to meet a
new microphone deadline. The voice queue prioritizes Whistle at next
admission opportunity but may timeout if a native call is already running.
The current `capture_active` flag begins at `transcribe_stream`, **not**
during upstream microphone framing/utterance collection. Admission therefore
does not guarantee that cold model startup cannot overlap pre-ASR audio
collection. Hardware tests must check microphone buffers, capture latency,
missed frames, GPU memory fragmentation and scheduler deadline misses. Scheduler/timer/tool
execution does not acquire the inference lock and stays independent. The
underlying Qwen ModelSupervisor already synchronizes its launch and never
kills a backend it adopted; this feature does not spawn a second model
server. Ctrl+C/`alfred close` shuts down the inference queue and safely
stops only owned processes; it never resubmits a tool after interruption.
SDK failure may retry a **model inference** via existing Qwen fallback only
before any System2 tool execution; an unauthorized or invalid model plan is
never retried as a new action.

## Reproducible offline matrix and provenance

Run from repository root:

```sh
PYTHONPATH=. python tests/phase5_qa.py --out artifacts/phase5-offline
python -m unittest discover -s tests -v
PYTHONPATH=. python tests/bench_needle_fastpath.py
```

The deterministic fixture generator `tests/phase5_qa.py` covers **12
semantic scenario families x 12 repeated/perturbed trials (144 trial IDs)**,
each executed in all **three** policy
configurations: baseline Needle→Qwen, Needle bounded feedback/context, and
a proxy for unified optional Harness+admission. Categories include
single GPU/status tools, two-step feedback, pronouns, timers, service
approvals, repeated calls after failed continuations, low-confidence,
malicious tool return, meeting transcript injection, reasoner offline
failure, and attempted plugin/network egress.

The comparison uses **real System2 preflight/approval queue, SQLite task
ledger and reminder engine**, but deliberately **mocks native Needle,
Whistle, Qwen, Harness, and device telemetry**. In the unified mode,
`resource_admission_enabled=true` is configured **before** Alfred's Qwen
client is created. Deterministic admitted Needle, Needle-feedback, Whistle,
Qwen and Harness stub calls enter the **real Condition-backed gate**.
The first eight trial IDs per family are nominal repetitions (not different
semantic tasks). The matrix includes explicit capture contention (variant 08), low available
RAM (09), excessive swap (10) and CPU load (11) per category, and its
raw CSV records gate-enabled, granted, queued, rejection, pressure and
resource-fixture fields for every one of the 432 cases. Some malformed
or stale scoped pronoun requests are safely rejected before reaching inference,
so such rows cannot be counted as an admission pressure-grant/refusal and are
reported separately. This validates
offline cooperative policy wiring only, not native performance or
real concurrent device behavior. The "unified" path is a
Harness/admission policy proxy, NOT full native latency. Phase 5 #16's
`ContextManager` is opt-in in BOTH enhanced arms and disabled in baseline;
the context-pronoun family seeds a referent only with a preceding actual
System2 fake-tool read, then checks typed observed provenance, owner/mode
isolation, TTL expiry and same-task Needle packet construction. These probes
are reported in separate raw columns/aggregate counts, including unified
admission pressure/capture denials. The native Jetson context/device boundary
is NOT evaluated by fake tools. `/proc/meminfo` and
normalized host CPU load are measured locally; **Jetson GPU load, power
and Qwen cold/warm model times are NULL**, never interpolated.

The script writes:
- `fixtures.json` — 144 stable trial IDs across 12 families, no personal data;
- `raw.csv` — one row per case/configuration (432 rows), actual local
  wall-clock policy/SQLite latency, task state, Qwen/Harness model-stub
  turn counts, stage/accepted tool counts, reminder side effects,
  model escalation rate, resource snapshots and per-fixture admission
  grants/rejections/pressure/capture probes, context verified seed,
  same-task packets, scope-isolation and TTL denial counters;
- `report.json` — measured aggregates with p50/p95, task-completion
  fraction and all critical safety gates;
- `report.md` — readable side-by-side comparison and caveats.

CI uploads these as a downloadable per-run artifact. **The artifact comes
from the actual CI runner and must not be represented as a real Orin
benchmark.** Every record carries `native_jetson=false` and explicit
`environment`. Reports from different runner hosts may vary heavily.

## Adversarial QA and hard release criteria

`tests/test_inference_admission.py` and
`tests/test_phase5_release.py` cover resource sampler failures, CPU/RAM/
swap pressure, bounded queue/Whistle priority, shutdown interrupting
waiters, no duplicate model startup, no uncertain tool side effects,
actual System2 preflight denial of network/shell/non-allowlisted tools,
and deterministic matrix output. Existing #13/#14/#15/#16/#17 and pinned SDK/Cordis
tests continue testing no unexpected native plugin permissions, local
loopback-only Qwen URLs, one-time local approval/replay and bounded
prompt injection. A separate integrated handoff/context/admission
security test validates same-task model packet and never turns skill metadata
into an executable tool. Passing these stubbed tests is not evidence of
native hardware security or device-specific deletion guarantees.

`alfred.release_gates.evaluate_release` makes release impossible
without all of the following:

1. Physical **NVIDIA Jetson Orin Nano 8GB** identification; actual offline
   native models and at least 100 meaningfully distinct (not just reindexed)
   scenarios with test-source
   provenance, all dependencies merged.
2. **Zero** unauthorized tool executions, unintended side-effect replays or
   plugin sandbox/egress success. Any positive/unknown result blocks release.
3. Native p50/p95 task latency, Qwen cold/warm load, peak RAM, peak swap,
   normalized CPU load and peak GPU utilization **measured**, with user-
   approved explicit maximum threshold for every metric. No invented or
   pre-populated bounds. Threshold violations block release.
4. Pass/fail evidence for Whistle capture, Phase 3 timers and scheduler
   deadlines, and clean owned-process shutdown under low memory/overload.
5. An explicit physical-device owner acknowledgment of hardware acceptance.

`require_release` raises `ReleaseBlocked` when any declared gate fails.
**Important provenance limitation:** `evaluate_release` is a strict
declaration/checklist validator, not a device attestation system. A caller
could supply invented `native_jetson_tested=true`, arbitrary source text,
fabricated metrics and a hardware-acknowledgment flag; the function cannot
verify these assertions independently. Release MUST also require external
hardware-owner signoff and review of genuine linked raw device run manifests
before enabling the feature. Offline CI cannot legitimately attest
`native_jetson_tested=true` or deliver a hardware release pass.
Do **not** default-enable Needle feedback, persistent context, Harness,
meeting access or admission based solely on this branch's green CI.

## Device experiment checklist (offline, repeat across days)

1. Record real JetPack/L4T, Python/Qiskit-independent toolchain, model GGUF/
   Whistle/Needle hashes, Jetson RAM and power mode, all backend configurations,
   git commit, ambient temperature and exact local clock.
2. Each day run at least **three independent cold starts** and warm sessions
   with baseline, Needle loop+scoped context, unified Harness+context. **Interleave
   mode order** to limit temperature and power bias. The 12 mock families are
   not an adequate real-hardware diversity suite: preregister >=100 distinct
   semantic tasks, with owner/source/precondition/adversarial variations and
   honest repeated trial labels. Consequential tasks must run against
   sandboxed allowed user services, not production actuators.
3. Capture full p50/p95 wall latency and truthful task outcomes; record cold/
   warm Qwen start latency from supervisor health probes, CPU/GPU utilization
   via approved tegrastats/sysfs read, max RAM/swap and timestamps while
   Whistle is streaming, microphone overrun/drop counts, Phase 3 reminder
   deadline lateness, and subprocess/server count.
4. Repeat with 1–N USB microphones, optional Bluetooth and actual Jetson
   SSD/PDF/OBEX adapter after #17 merges. Verify no private historical
   transcript leakage, calibrated overlap/cross-talk uncertainty and no
   invented MoM owners/dates or unverified transfer success.
5. Test intentional server crashes while read-only and approved service
   tasks are in flight. Verify one-time executed side effects are NEVER
   retried; audit SQLite approvals and all child PIDs. Simulate low available
   RAM, swap pressure, high CPU and Ctrl+C/close while Whistle records.
6. Archive **raw** native CSV/JSON and model-side traces stripped of private
   audio/transcripts; review thresholds with actual owner. Use
   `evaluate_release(native_report, approved_thresholds,
   acknowledge_hardware=True)` only after independent review.

No native benchmark, hardware test, model-speed claim, plugin sandbox proof
for installed Jetson extras, or production release approval is implied.
