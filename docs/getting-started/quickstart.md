# Quickstart

A first run in five steps, from checking the setup to speaking a command. It assumes you
completed [Installation](installation.md) and have a `config.json`.

## 1. Check the setup

```bash
alfred --config config.json --doctor
```

`--doctor` reports audio sources, the Bluetooth card profile, the inference backend and the
Harness audit. Read the output for these lines:

| Line | Meaning |
|---|---|
| `[CANDIDATE]` / `[REJECTED]` | Each recording source, with the reason when rejected. |
| `MICROPHONE INPUT STREAM VERIFIED` | Real PCM bytes arrived from the selected source. |
| `Harness NOT READY` | Qwen is not reachable or the Harness failed. See [Troubleshooting](../guides/troubleshooting.md). |
| `Cordis tool bridge: NOT YET WIRED` | Expected. The Harness has no model-facing tools by design. |

Exit status 2 means no usable microphone or a failed audio or Harness check.

## 2. Start the inference backend

```bash
alfred --config config.json --ensure-backend
```

This adopts a compatible server already running on the configured loopback port, or starts the
configured backend. It prints the backend status as JSON and exits. The server it starts stays
running. See [Inference backends](../guides/inference-backends.md).

## 3. Try text mode

Text mode needs no microphone and no wake word:

```bash
alfred --config config.json --text "show GPU status"
```

Needle routes the request, Python validates it, and the tool runs. Read-only tools run
immediately.

## 4. Plan and approve

Ask the Qwen planner without loading any speech model:

```bash
alfred --config config.json --reason "Schedule a GPU status check every 10 minutes"
alfred --config config.json --pending
alfred --config config.json --approve 1
```

A sensitive plan is **staged**, not executed. Review it with `--pending` and run it once with
`--approve ID`, or discard it with `--reject ID`. Approval is possible only from the CLI. See
[Approvals](../guides/approvals.md).

## 5. Listen

```bash
alfred --config config.json
```

Say **"Alfred, show GPU status"**. Speech without the wake word is ignored. To stop, say
**"Alfred, close"** or run this from another terminal:

```bash
alfred close
```

## Where to go next

- Keep it running across reboots: [Deployment](../guides/deployment.md).
- Set reminders and GPU watches: [Automations](../guides/automations.md).
- Pair the Sony headset or tune the microphone: [Hardware setup](hardware-setup.md).
- Understand what happens to a request: [Request flow](../architecture/request-flow.md).
