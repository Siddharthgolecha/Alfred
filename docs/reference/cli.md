# CLI reference

Alfred has one entry point. Both forms below are equivalent:

```bash
alfred [options]
python -m alfred [options]
```

With no operation flag, Alfred starts **voice mode**. Source:
[`alfred/cli.py`](../../alfred/cli.py).

## Global options

| Option | Meaning |
|---|---|
| `--config PATH` | JSON config file. Default `config.json`. If the file does not exist, built-in defaults are used. See [Configuration](configuration.md). |
| `--resume-session SESSION_ID` | Reuse a prior local session ID. Interrupted tools are never replayed. Allowed only with voice mode, `--text` and `--reason`. |

## Operations

The operations below are **mutually exclusive**: pass at most one. The positional `close`
command is separate and cannot be combined with any of them.

### Run

| Operation | Meaning |
|---|---|
| *(none)* | Voice mode. Checks the microphone first, then runs the listener. |
| `--text TEXT` | Run one command through Needle and tools, then exit. No wake word is needed and the voice control socket is not taken. |
| `--reason TEXT` | Ask the Qwen planner to plan `TEXT`. No Cactus models are loaded. Prints the result as JSON. |
| `--scheduler-only` | Run reminders and watches without a microphone, Cactus or Qwen. Binds the `scheduler` control socket. Used by [`alfred-scheduler.service`](../guides/deployment.md). |

### Diagnose

| Operation | Meaning |
|---|---|
| `--doctor` | Report audio sources, Bluetooth cards, inference backend, and Harness audit. Starts a real Harness child when Qwen is ready. |
| `--ensure-backend` | Start or verify the selected inference backend, print its status as JSON, and exit. `--ensure-llama` is an alias. |
| `--skills` | Print available and unavailable capabilities as JSON. See [Skills and meetings](skills-and-meetings.md). |
| `--automations` | List all automations as JSON (including paused and cancelled). |
| `--automation-tick` | Run one scheduler cycle and exit. For debugging. |

### Approvals

These are CLI-only. Voice cannot approve. See [Approvals](../guides/approvals.md).

| Operation | Meaning |
|---|---|
| `--pending` | List proposed actions awaiting approval. |
| `--approve ID` | Approve and execute proposal `ID` once. |
| `--reject ID` | Reject proposal `ID`. |

### Retained facts

All three require `runtime.context_enabled` **and** `runtime.context_persistence_enabled`.
Otherwise the command fails with a usage error.

| Operation | Meaning |
|---|---|
| `--memory-set KEY=VALUE` | Retain a short preference. Splits on the first `=`. |
| `--memory-list` | List retained facts for the configured owner and mode. |
| `--memory-clear KEY\|ALL` | Delete one fact, or all facts in the scope. |

### Workflows

See the [Workflows guide](../guides/workflows.md). All of these print JSON.

| Operation | Meaning |
|---|---|
| `--minutes` | Directly queue the approved `meeting_minutes` workflow and exit. Equivalent to `--workflow-start meeting_minutes`. Prints the queued run as JSON. |
| `--workflow-draft JSON_FILE` | Validate and save a graph as an **unapproved** draft. The file must be local and at most 40 000 bytes. |
| `--workflow-approve NAME` | Approve the validated graph version. |
| `--workflow-list` | List definitions and their approval state. |
| `--workflow-start NAME` | Queue an approved workflow. |
| `--workflow-runs` | List recent runs. |
| `--workflow-status RUN_ID` | Show durable step results for a run. |
| `--workflow-cancel RUN_ID` | Cancel the pending steps of a run. |
| `--workflow-tick` | Execute up to three ready steps. |
| `--workflow-reconcile RUN_ID` | Mark an uncertain in-flight step as interrupted. |
| `--workflow-schedule NAME:MINUTES` | Run an approved read-only workflow on an interval. Splits on the last `:`. |
| `--workflow-triggers` | List recurring triggers. |
| `--workflow-trigger-pause TRIGGER_ID` | Pause a trigger. |
| `--workflow-trigger-resume TRIGGER_ID` | Resume a trigger. |
| `--workflow-trigger-cancel TRIGGER_ID` | Cancel a trigger. |

## The `close` command

```bash
alfred close
```

Asks running Alfred processes to shut down through their control sockets, first `voice`, then
`scheduler`. Prints `Alfred stopped: voice, scheduler` (listing only those that stopped) or
`Alfred is not running`. See [Concurrency and shutdown](../architecture/concurrency.md).

`close` cannot be combined with `--resume-session` or with `--doctor`, `--text`,
`--ensure-backend`, `--scheduler-only`, `--automations`, `--automation-tick`, `--reason`,
`--skills`, `--pending`, `--approve`, `--reject`, or any `--memory-*` flag. Combining it with
one of those exits with a usage error.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. Also returned after Ctrl+C or SIGTERM. |
| `1` | `--reason` produced an `incomplete` plan (no valid action). |
| `2` | The microphone is unavailable or disconnected (voice mode), or `--doctor` found a problem with audio or the Harness. |

Argument errors from `argparse` exit with its standard status. Unhandled exceptions propagate
as a Python traceback.

`alfred.service` sets `RestartPreventExitStatus=2`, so systemd does not restart-loop on a
missing microphone.

## Signals

`SIGTERM` is converted to `KeyboardInterrupt`, so `finally` blocks run and children are
stopped. A previously installed handler is restored on exit.

## Examples

```bash
# Check the setup without loading models
alfred --config config.json --doctor

# One text command
alfred --config config.json --text "show GPU status"

# Plan without the microphone; review and approve locally
alfred --config config.json --reason "Restart the scheduler"
alfred --config config.json --pending
alfred --config config.json --approve 7

# Retain a preference (needs context persistence enabled)
alfred --config config.json --memory-set unit_preference=celsius
```
