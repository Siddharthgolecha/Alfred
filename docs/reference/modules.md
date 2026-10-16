# Module map

One line per source file. Paths are relative to the repository root. For how the pieces fit
together, see the [Architecture overview](../architecture/overview.md).

## Entry and composition

| File | Purpose |
|---|---|
| [`alfred/__main__.py`](../../alfred/__main__.py) | Enables `python -m alfred`. Calls `main()`. |
| [`alfred/app.py`](../../alfred/app.py) | `Alfred`, the composition root. Builds and closes every service. Re-exports `doctor`, `_main` and `main` from `cli.py`. The `alfred` console script points here. |
| [`alfred/cli.py`](../../alfred/cli.py) | Argument parsing, `--doctor` diagnostics, exit codes, SIGTERM handling. See [CLI](cli.md). |
| [`alfred/session.py`](../../alfred/session.py) | `SessionManager`: session ledger, context manager and Needle session slots. |
| [`alfred/lifecycle.py`](../../alfred/lifecycle.py) | `ShutdownSocket` and `request_close` for `alfred close`, plus `ChildProcesses` for owned subprocesses. |

## Voice path

| File | Purpose |
|---|---|
| [`alfred/audio.py`](../../alfred/audio.py) | `AudioManager`: PulseAudio/PipeWire capture, BlueZ headset profile negotiation, live-PCM verification. |
| [`alfred/listener.py`](../../alfred/listener.py) | `WakeWordListener`: the capture, wake, transcribe loop. |
| [`alfred/wakeword.py`](../../alfred/wakeword.py) | `WakeWordGate` (transcript prefix and follow-up window) and the optional `AcousticWakeDetector` (openWakeWord ONNX). |
| [`alfred/console.py`](../../alfred/console.py) | In-place typing-style preview of the Whistle transcript. |
| [`alfred/feedback.py`](../../alfred/feedback.py) | Text and optional `spd-say` speech output. |

## Routing and models

| File | Purpose |
|---|---|
| [`alfred/router.py`](../../alfred/router.py) | `RequestRouter`: Needle dispatch, confidence checks, Qwen handoff and completion records. |
| [`alfred/routing.py`](../../alfred/routing.py) | Compatibility shim re-exporting two helpers from `router.py`. |
| [`alfred/models.py`](../../alfred/models.py) | `Cactus` (Needle and Whistle wrappers), `Qwen` (schema-constrained planner client), `request_json`. Also exports `LlamaSupervisor`, an alias of `ModelSupervisor`. |
| [`alfred/system.py`](../../alfred/system.py) | `System`: common inference-admission foundation for System1 and System2. |
| [`alfred/system1.py`](../../alfred/system1.py) | `System1`: Needle 3 fast-path router; retains `Needle3Engine` as a compatibility alias. |
| [`alfred/backends.py`](../../alfred/backends.py) | `ModelSupervisor`: detects, adopts or starts `llama.cpp`, Ollama or LM Studio. |
| [`alfred/system2.py`](../../alfred/system2.py) | `System2` and `Approvals`: local reasoning, plan preflight, approval store and one-shot execution. |
| [`alfred/inference_admission.py`](../../alfred/inference_admission.py) | `InferenceAdmission`: single-slot gate with memory, swap and CPU pressure checks. |

## Tools and automation

| File | Purpose |
|---|---|
| [`alfred/runtime.py`](../../alfred/runtime.py) | `TOOL_SCHEMAS`, `validate`, `validate_plan`, `Store` (SQLite), `Tools` (handlers), `Scheduler`. See [Tools](tools.md). |
| [`alfred/automations.py`](../../alfred/automations.py) | Durable one-off and daily reminders and GPU temperature watches with an outbox. |
| [`alfred/workflow_engine.py`](../../alfred/workflow_engine.py) | Durable, bounded, acyclic, allowlisted workflow graphs with triggers. |
| [`alfred/skills.py`](../../alfred/skills.py) | Non-executable capability inventory. See [Skills and meetings](skills-and-meetings.md). |
| [`alfred/meeting_adapter.py`](../../alfred/meeting_adapter.py) | `OfflineMeetingAdapter`: consent, permits and cleanup around an injected provider. |
| [`alfred/meeting_models.py`](../../alfred/meeting_models.py) | `MeetingPermit`, `sanitize_segments`, `validate_minutes_proposal`. |
| [`alfred/meeting_cleanup.py`](../../alfred/meeting_cleanup.py) | `MeetingCleanupJournal`: SQLite table of opaque cleanup tokens. |
| [`alfred/release_gates.py`](../../alfred/release_gates.py) | `evaluate_release` and `require_release`: a checklist that blocks rollout without real Jetson measurements. |

## Shared support

| File | Purpose |
|---|---|
| [`alfred/config.py`](../../alfred/config.py) | Defaults, loading, environment overrides, validation and the Pydantic config models. See [Configuration](configuration.md). |
| [`alfred/constants.py`](../../alfred/constants.py) | Named limits, timeouts, TTLs, truncation lengths and compiled regex patterns. |
| [`alfred/validators.py`](../../alfred/validators.py) | Small reusable type, range, TTL, string and JSON helpers. |
| [`alfred/exceptions.py`](../../alfred/exceptions.py) | `AlfredError`, the base class for Alfred exceptions. |

## Cognitive layer (`alfred/cognitive/`)

See [Cognitive layer](../architecture/cognitive.md) and
[DeepSeek Harness](../architecture/harness.md).

| File | Purpose |
|---|---|
| [`__init__.py`](../../alfred/cognitive/__init__.py) | Public exports of the contract types. |
| [`contracts.py`](../../alfred/cognitive/contracts.py) | Seven versioned, size-bounded Pydantic event types and a strict JSON codec. |
| [`sessions.py`](../../alfred/cognitive/sessions.py) | Append-only SQLite (WAL) ledger of tasks and events. |
| [`context.py`](../../alfred/cognitive/context.py) | Three-tier bounded context: task observations, session slots, approved facts. |
| [`needle_loop.py`](../../alfred/cognitive/needle_loop.py) | Bounded Needle feedback loop with an isolated native conversation per task. |
| [`escalation.py`](../../alfred/cognitive/escalation.py) | `EscalationEnvelope`: the bounded Needle to Qwen handoff. |
| [`harness_sdk.py`](../../alfred/cognitive/harness_sdk.py) | `LocalHarness`: the required adapter around the DeepSeek Harness SDK. |
| [`harness_config.py`](../../alfred/cognitive/harness_config.py) | Pinned SDK and runtime versions and manifest constants. |
| [`harness_audit.py`](../../alfred/cognitive/harness_audit.py) | Audit of the Cordis plugin graph against the Alfred overlay. |
| [`harness_bridge.py`](../../alfred/cognitive/harness_bridge.py) | Frame encoding and decoding that projects validated Alfred contracts for the Harness. Not a privileged tool channel. |
| [`alfred_safe.patch.yml`](../../alfred/cognitive/alfred_safe.patch.yml) | Cordis overlay that disables unsafe plugin rows. |
