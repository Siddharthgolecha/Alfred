# Concurrency and shutdown

Alfred is a threaded Python process with a few owned child processes. This page lists them,
the locks that protect shared state, the inference admission gate and the shutdown order.

## Threads and processes

| Name | Kind | Started by | Purpose | Stops on |
|---|---|---|---|---|
| main | thread | `cli.main` | Voice loop (`WakeWordListener.listen`) or one-shot command | Exit, `KeyboardInterrupt` |
| `alfred-scheduler` | daemon thread | `Scheduler.start` | Every 2 s: deliver due reminders, run due schedules, tick automations | `Scheduler.stop` (join, `Timeouts.SCHEDULER_STOP`) |
| `alfred-workflow-worker` | daemon thread | `Scheduler.start` | Every 2 s: advance workflow runs. Separate so a 60 s named task cannot delay timers | same |
| `model-supervisor` | daemon thread | `Alfred.start_services` | Every `qwen.supervisor_poll_seconds`: ensure the backend is up | `_watchdog_stop` |
| `alfred-close-listener` | daemon thread | `ShutdownSocket.__enter__` | Accept `close` on the Unix socket | `__exit__` |
| `alfred-harness-turn` | daemon thread | `LocalHarness._run_admitted` | Runs one SDK turn so the parent can enforce a timeout | Turn completes or times out |
| `parec` | child process | `AudioManager._open` | 16 kHz mono PCM capture | `AudioManager.close` |
| inference server | child process group | `ModelSupervisor` | llama.cpp, llama-server or Ollama, only if Alfred started it | `ModelSupervisor.close` |
| `dsh` (Cordis) | child process | DeepSeek SDK | Planning runtime | `LocalHarness.close` |
| approved task or `spd-say` | short-lived children | `ChildProcesses.run` | Named tasks, services, TTS | Completion, timeout, or `ChildProcesses.close` |

In `--scheduler-only` mode only the scheduler threads and the close listener run. No microphone,
Cactus or Qwen is loaded.

## Locks and shared state

| Lock | Guards | Notes |
|---|---|---|
| `Store.lock` (`RLock`) | The shared SQLite connection | Held with `with store.lock, store.db` for a transaction |
| `RequestRouter._needle_execution_lock` (`RLock`) | One task at a time | `handle_text` holds it for the whole task |
| `RequestRouter._needle_cancel` (`Event`) | Cooperative cancel | Checked between loop steps and set at shutdown |
| `ModelSupervisor.lock` (`RLock`) | Backend start and stop | `close()` sets `_stopping` first to wake `_wait_ready` |
| `LocalHarness._lock` (`RLock`) | SDK open, turn and close | |
| `ContextManager._lock` (`RLock`) | Slots, task observations, facts | One lock for read and write, so a scope switch mid-task cannot mix scopes |
| `ChildProcesses._lock` and `_stopping` | Child registry | After `close()` starts, new subprocesses are refused |
| `InferenceAdmission._cond` | Admission queue | See below |

## Inference admission

`InferenceAdmission` ([`inference_admission.py`](../../alfred/inference_admission.py)) is a
single-slot FIFO gate that serialises expensive native inference on an 8 GB shared-memory device.
It is a thread-level gate, not a GPU scheduler: it cannot interrupt a native call that already
started. Controlled by `runtime.resource_admission_enabled` (default true).

Kinds: `whistle`, `needle`, `needle_feedback`, `qwen`, `qwen_explain`, `harness`.

```{mermaid}
flowchart TD
    req["claim(kind)"] --> en{"enabled?"}
    en -- no --> go["run immediately"]
    en -- yes --> full{"queue slot free?<br/>2 for whistle, 1 for others"}
    full -- no --> rej1["AdmissionRejected: queue full"]
    full -- yes --> wait["Wait for turn<br/>(max resource_max_wait_ms)"]
    wait --> turn{"My turn?<br/>slot free, whistle first,<br/>others wait while capture is active"}
    turn -- timeout --> rej2["AdmissionRejected: queue busy"]
    turn -- yes --> pres{"Resource pressure?"}
    pres -- "available RAM below min,<br/>or swap above max" --> rej3["AdmissionRejected"]
    pres -- "CPU load per core above max<br/>(not for whistle)" --> rej3
    pres -- "unmeasurable" --> rej3
    pres -- ok --> run["Run; release on exit"]
```

Behaviour that is easy to miss:

- **Voice has priority.** While `WakeWordListener` is transcribing it calls
  `set_capture_active(True)`. Qwen and Harness requests then wait or are refused, and the
  background Qwen watchdog skips its check.
- **Whistle ignores CPU load** but still refuses critical RAM or swap exhaustion.
- **Unknown headroom is a refusal.** If `/proc/meminfo` cannot be read, admission is rejected
  instead of assuming memory is free.
- **No retries.** A rejected or failed request is not retried by the gate.
- **Tools and the scheduler never claim a slot.**
- Optional diagnostics are dropped, not queued, when the gate is busy or under pressure.

Thresholds are in the [configuration reference](../reference/configuration.md#runtime).

## Startup order

1. `cli._main` parses arguments and loads config.
2. For voice mode, `AudioManager.ensure_input()` runs **before** any model loads. No live PCM
   means exit code 2.
3. `ShutdownSocket(logs, "voice")` binds the control socket. A live instance of the same role
   makes this fail.
4. `Alfred(config)` builds all services and loads Needle and Whistle.
5. `require_cognitive_runtime()` audits the Harness, starts or adopts Qwen, and starts the SDK.
6. `start_services()` starts the scheduler threads and the model watchdog.
7. `listen()` enters the voice loop.

## Shutdown

Entry points: Ctrl+C, `SIGTERM` (translated into `KeyboardInterrupt` by `cli.main`), the spoken
command "Alfred, close", or `alfred close` over the control socket (which sends itself `SIGTERM`).
All of them unwind through the same `finally: app.close()`.

`Alfred.close()` is idempotent and runs in this order:

```{mermaid}
flowchart TD
    a["1. admission.close()<br/>reject new inference"] --> b["2. watchdog stop event"]
    b --> c["3. router._needle_cancel.set()"]
    c --> d["4. session_manager.close()<br/>clear slots and session context"]
    d --> e["5. audio capture (parec)"]
    e --> f["6. harness subprocess"]
    f --> g["7. inference backend<br/>only if Alfred started it"]
    g --> h["8. ChildProcesses.close()<br/>tasks and speech"]
    h --> i["9. scheduler threads"]
    i --> j{"Worker threads exited?"}
    j -- yes --> k{"Active command still<br/>holds the lock?"}
    k -- no --> l["Release session ownership;<br/>close SQLite"]
    k -- yes --> m["Keep ownership; log a warning"]
    j -- no --> n["Leave DB open until process exit;<br/>log a warning"]
```

Each step in 5–9 runs inside its own `try/except`, so one failure does not skip the rest.

### What is and is not stopped

| Resource | On shutdown |
|---|---|
| `parec`, approved tasks, `spd-say` | Terminated (SIGTERM, then SIGKILL after 2 s) |
| Inference server Alfred started (llama.cpp, llama-server, Ollama) | Process group terminated (SIGTERM, then SIGKILL after 5 s) |
| LM Studio Alfred started | `lms server stop` |
| An inference server that was already running (**adopted**) | **Never touched** |
| A server started by `--ensure-backend` | Left running by design |
| Effects of earlier `control_service` actions | Not undone |
| In-flight tool | Allowed to finish within its own timeout; no rollback |

With systemd, `alfred close` exits with status 0, so `Restart=on-failure` does not restart it.
Use `systemctl --user stop alfred` to mark the unit stopped. Exit code 2 (no microphone) is
excluded from restarts by `RestartPreventExitStatus=2`.
