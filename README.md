# Alfred

Offline-first, continuous English voice automation for the **NVIDIA Jetson Orin Nano 8GB**
(target: JetPack 7.2.1 / CUDA 13.2, not yet validated).

```text
Microphone → wake word → Whistle STT → Needle 3 tool router → validated Python tools
                                              │
                                              └── complex requests → Qwen 3.5 4B (local) via DeepSeek Harness
```

- **Offline by design.** Speech recognition, routing and planning all run on the device.
- **Models propose, Python decides.** Needle 3 and Qwen only emit structured calls. Python validates every
  action and argument before anything runs. There is no arbitrary shell access.
- **Wake-word gated.** Speech not addressed to "Alfred" never reaches a model or a tool.
- **Local approval for sensitive actions.** Voice alone cannot approve a privileged action.

> **Status:** unit-tested offline, **not yet validated on Jetson hardware** (Bluetooth profiles, native
> Cactus ARM64 binaries, latency and memory, spoken feedback). See
> [Testing](docs/development/testing.md) for what CI does and does not prove.

## Quick start

## Phase 2 planning and approvals

Alfred includes schema-grounded Qwen planning and local approvals for sensitive commands. The integrated security design and CLI examples are documented in [Phase 2](docs/PHASE2.md). These commands are available without microphone input: `--reason`, `--pending`, `--approve ID`, and `--reject ID`. Voice-only speech cannot approve a privileged action.

**Qwen planner troubleshooting (issue #24):** On llama.cpp, Alfred requests non-thinking JSON output because some Qwen3.5 chat templates produce incomplete or schema-breaking plans when thinking consumes the output budget. A malformed plan gets one generation-only retry. If neither response matches the schema, Alfred reports that no action was taken; it will **not** claim a schedule was saved or run a partially parsed step. Test the local endpoint with `python -m alfred --config config.json --reason "Schedule a GPU status check every 10 minutes"`. If it repeatedly fails, verify the model/chat-template compatibility, inspect local backend logs, and check `qwen.max_tokens`. Sensitive schedules still require `--approve ID`; a vague request such as "create a schedule" needs an action and interval.

## Phase 3 — automations and typing-style speech

Phase 3 adds [persistent absolute/daily reminders and GPU-temperature watches](docs/PHASE3.md), with SQLite notification delivery and a headless `--scheduler-only` service that works without a microphone. Sensitive recurring changes still need local approval. Run `python -m alfred --automations` to inspect jobs.

Whistle now updates **one terminal line like typing** as new speech arrives. Corrections revise only the changed suffix. It does not print a new line for every partial phrase; the final transcript is committed and used for tool execution. Non-TTY logs only print the final transcript.

## Phase 4 — Durable workflow graphs

Phase 4 introduces [approved, persistent workflow DAGs](docs/PHASE4.md), conditional branches, bounded read-only retries, safe cancellation and restart-aware execution. Use `--workflow-draft` and `--workflow-approve` locally to register approved graphs, `--workflow-start` to run them, and `--workflow-status` to inspect results. Read-only graphs can also run on recurring schedules. Voice requests to run these workflows still require Phase 2 local approval. The `intrinsic_jetson` branch remains separate.

## Phase 5 — Maestaris orchestration and DeepSeek Harness roadmap

Phase 5 is organized as [detailed, dependency-linked GitHub Issues](https://github.com/Siddharthgolecha/Alfred/issues/11), with Maestaris Worker A implementing one ready issue at a time and a separate reviewer assessing PR diffs and CI before merging. The workflow follows `AGENTS.md` and `coordination/maestaris.yaml`. See [Phase 5 Maestaris and main-to-Jetson sync](docs/PHASE5_MAESTARIS.md).

Every new `main` commit triggers a **non-destructive tested merge** into `intrinsic_jetson`. Merge conflicts and failing tests create an issue rather than overwriting branch-specific code.

## Jetson-specific SSD and meeting workflows

The separate [`intrinsic_jetson` branch](https://github.com/Siddharthgolecha/Alfred/tree/intrinsic_jetson) adds guarded ext4 USB/SATA SSD model/cache storage, read-only swapfile health checks, and an offline multi-microphone Minutes of Meeting generator. It automatically detects and counts working USB-hub/Bluetooth microphones, records independent PCM channels, applies optional acoustic clap calibration, retains Whistle word-level timing, annotates overlapping speech and potential cross-talk, and shows source-labeled transcripts in the CLI (optional concurrent `--live-preview`). It then gives a chronological transcript to Qwen 3.5 4B for structured minutes, saves a PDF, generates a QR handoff, and optionally pushes via paired-device Bluetooth OBEX. **Swap does not execute inference; QR alone does not initiate Bluetooth transfer.** See [Jetson-specific setup and safety](docs/INTRINSIC_JETSON.md).

## What is implemented

- Sony **WI-C100** Bluetooth microphone using HFP/HSP (A2DP alone does *not* provide headset mic capture).
- `auto` / `bluetooth` / `direct` selection; auto prefers the Sony mic and falls back to a direct microphone.
- BlueZ + PipeWire-Pulse/PulseAudio discovery, profile switching, explicit source capture with `parec`, reconnection attempts if a MAC address is configured.
- Fail-fast microphone health check: reject unconfirmed Jetson "Built-in Audio" inputs, reject monitor sources and unavailable ports, require actual timed PCM delivery before loading the models.
- 16 kHz mono PCM capture, tunable RMS VAD, incremental Whistle transcription; **only wake-authorized, finalized utterances** can trigger tools.
- Default "Alfred" wake phrase, single bounded follow-up, and optional local openWakeWord ONNX pre-transcription detection.
- Needle 3 structured calls; no direct execution by the model; Python validates each action and its arguments.
- Strict allowlists for `systemctl --user` services and fixed named script argv; **no arbitrary shell**.
- System diagnostics, SQLite reminders, recurring read-only status polls, Qwen multi-step plans, saved named workflows.
- Lightweight optional local speech feedback using `spd-say`.
- Backend-aware model discovery, local health checks and managed startup for `llama serve`, legacy `llama-server`, Ollama and LM Studio; no duplicate spawn onto an occupied port.

**Not yet hardware-validated:** Bluetooth audio profile support on your particular JetPack image, the native Cactus ARM64 binaries, speed/memory on Jetson, and spoken feedback. Run the checklist below directly on the board.

## Installation on the Jetson

The board should have a working Bluetooth adapter, BlueZ, and an audio server with a PulseAudio-compatible socket (PulseAudio or PipeWire's `pipewire-pulse`).

```bash
git clone https://github.com/Siddharthgolecha/Alfred.git
cd Alfred
# Use this branch for the Jetson-only meeting and SSD features:
git switch intrinsic_jetson
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'

cp config.example.json config.json

python -m alfred --config config.json --doctor                  # microphone, backend and Harness health
python -m alfred --config config.json --text "Show my system status"
python -m alfred --config config.json                           # listen continuously
```

For optional acoustic wake-word detection on Linux Python 3.12+, use
`uv sync --extra wakeword` rather than `pip install -e '.[wakeword]'` to avoid
upstream openWakeWord's unused TFLite dependency. If your active virtualenv
is outside `.venv`, use `uv sync --active --extra wakeword`.

Full instructions: [Installation](docs/getting-started/installation.md) and
[Quickstart](docs/getting-started/quickstart.md).

## Documentation

| I want to… | Read |
|---|---|
| Install and run Alfred | [Getting started](docs/getting-started/installation.md) |
| Configure a microphone, backend, wake word or service | [Guides](docs/guides/inference-backends.md) |
| Understand how it works | [Architecture overview](docs/architecture/overview.md) |
| Look up a CLI flag, config key or tool | [Reference](docs/reference/cli.md) |
| Fix a problem | [Troubleshooting](docs/guides/troubleshooting.md) |
| Contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See what changed | [CHANGELOG.md](CHANGELOG.md) |
| Report a vulnerability | [SECURITY.md](SECURITY.md) |

Build the HTML documentation locally:

```bash
pip install -e '.[docs]'
make -C docs html        # output: docs/_build/html
```

## Branches

`main` is the integration branch. Every new `main` commit is merged, non-destructively, into
`intrinsic_jetson` after tests pass. See [Maestaris workflow](docs/development/maestaris.md).

## License

Not yet chosen.
