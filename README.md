# Alfred — Jetson voice automations

Offline-first, continuous English voice automation on **NVIDIA Jetson Orin Nano 8GB** (JetPack 7.2.1 / CUDA 13.2).

**Microphone → Cactus Whistle streaming STT → Cactus Needle 3 tool router → validated Python tools.**
Needle can delegate complex commands to a **Qwen 3.5 4B Bartowski Q4_K_M GGUF** model hosted by local **llama.cpp**. The app adopts a healthy existing `llama-server`, or starts it if absent.

## What is implemented

- Sony **WI-C100** Bluetooth microphone using HFP/HSP (A2DP alone does *not* provide headset mic capture).
- `auto` / `bluetooth` / `direct` selection; auto prefers the Sony mic and falls back to a direct microphone.
- BlueZ + PipeWire-Pulse/PulseAudio discovery, profile switching, explicit source capture with `parec`, reconnection attempts if a MAC address is configured.
- Fail-fast microphone health check: reject unconfirmed Jetson "Built-in Audio" inputs, reject monitor sources and unavailable ports, require actual timed PCM delivery before loading the models.
- 16 kHz mono PCM capture, tunable RMS VAD, incremental Whistle transcription; **only finalized utterances** can trigger tools.
- Needle 3 structured calls; no direct execution by the model; Python validates each action and its arguments.
- Strict allowlists for `systemctl --user` services and fixed named script argv; **no arbitrary shell**.
- System diagnostics, SQLite reminders, recurring read-only status polls, Qwen multi-step plans, saved named workflows.
- Lightweight optional local speech feedback using `spd-say`.
- `llama-server` `/health` checking, model-id sanity check, managed startup and periodic watchdog recovery, process logging, no duplicate spawn onto an occupied port.

**Not yet hardware-validated:** Bluetooth audio profile support on your particular JetPack image, the native Cactus ARM64 binaries, speed/memory on Jetson, and spoken feedback. Run the checklist below directly on the board.

## Installation on the Jetson

The board should have a working Bluetooth adapter, BlueZ, and an audio server with a PulseAudio-compatible socket (PulseAudio or PipeWire's `pipewire-pulse`).

```bash
sudo apt update
sudo apt install -y bluez pulseaudio-utils libasound2-dev python3-venv
git clone https://github.com/Siddharthgolecha/Alfred.git
cd Alfred
# Until the PR merges, use:
git switch feat/jetson-voice-agent
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp config.example.json config.json
```

**Config:** set `qwen.model_path` to the absolute GGUF location, and `qwen.binary` to your `llama-server` executable (or put it in PATH). Example model: Bartowski **Qwen3.5-4B-Q4_K_M.gguf**. For an existing server set `qwen.url` to its local listen address (e.g. `http://127.0.0.1:8080`). The model path is required only if Alfred must start the server itself. The watchdog checks health every `qwen.supervisor_poll_seconds` (default 30) while Alfred runs. Optional environment overrides: `ALFRED_GGUF`, `ALFRED_LLAMA_SERVER`, `ALFRED_LLAMA_URL`.

```bash
# Populate the native model caches before going offline:
needle download needle3
needle download whistle

# Unit tests (no mic, Jetson or GGUF required):
python -m unittest discover -s tests -v

# Check available audio sources and the llama.cpp endpoint:
python -m alfred --config config.json --doctor
# Exit code 2 means no usable mic; source names by themselves are not enough.

# Ensure llama.cpp is healthy; if absent, start it detached:
python -m alfred --config config.json --ensure-llama

# Try one command in text mode:
python -m alfred --config config.json --text "Show my system status"

# Start listening continuously:
python -m alfred --config config.json
```

If using `llama.cpp` on Orin with CUDA, a typical native build is:

```bash
cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87
cmake --build llama.cpp/build --config Release -j4
# Set qwen.binary to the resulting llama.cpp/build/bin/llama-server
```

Start with context 4096, parallelism 1; change `qwen.gpu_layers` downward if 8GB shared RAM becomes constrained. Alfred **does not terminate** a server already running on the configured port. If that port serves a different model, it refuses Qwen requests rather than overwriting the process.

## Missing microphone / misleading "Built-in Audio" (bug-fix)

On some Orin Nano installations, PipeWire/PulseAudio lists **Built-in Audio** even though no microphone is connected to the developer kit. The name may describe an onboard audio controller rather than a connected microphone. Alfred **does not treat the label as proof**.

- \`alfred --doctor\` now reports each advertised recording source as \`[CANDIDATE]\` or \`[REJECTED]\`, including a reason. It attempts a short PCM read from the preferred source and reports \`MICROPHONE INPUT STREAM VERIFIED\` *only if recording bytes arrive*. It exits with status **2** otherwise.
- Ordinary continuous listening **fails at startup with exit code 2** when no microphone can capture audio. Cactus models and llama-server watchdog are not started in that case. The agent no longer pretends to listen while endlessly logging "No approved microphone".
- Auto mode prefers Sony WI-C100 HFP/HSP, then an operational USB/direct microphone. If a candidate cannot deliver audio, a subsequent eligible candidate is probed before failure.
- Generic onboard/SoC audio input is rejected from automatic selection, even if an input port is advertised, because that does not prove a microphone is physically attached. If a genuine microphone is wired to an onboard audio device, explicitly set \`audio.direct_source\` after inspecting \`pactl list sources\`; a live PCM probe is *still mandatory*. Explicitly selected ports known to be unavailable are never accepted.
- If audio disconnects while listening, Alfred attempts to switch to another operational candidate. If none is available, or repeated capture failures persist, it exits nonzero instead of retrying forever. If run under systemd with \`Restart=on-failure\`, the service manager may retry launching Alfred until a mic returns.
- \`audio.probe_timeout_seconds\` and \`audio.read_timeout_seconds\` default to 5 seconds each. Silence in a working stream is not a failure—PCM availability is only a functional check, not a physical-presence guarantee for every driver.

Useful diagnostics:

\`\`\`bash
pactl -f json list sources
pactl -f json list cards
python -m alfred --config config.json --doctor
echo $?   # 0: functional microphone / 2: no valid input
\`\`\`

\`--text\` and \`--ensure-llama\` do not need a microphone and remain available for diagnostics.

## Pair and select Sony WI-C100

1. Put WI-C100 in pairing mode. It is a Bluetooth neckband headset; its USB-C port is for charging, not a wired microphone.
2. Pair/trust and connect using BlueZ:
   ```bash
   bluetoothctl
   power on
   agent on
   default-agent
   scan on
   # After the Sony MAC is visible:
   pair XX:XX:XX:XX:XX:XX
   trust XX:XX:XX:XX:XX:XX
   connect XX:XX:XX:XX:XX:XX
   quit
   ```
3. Put the paired Sony MAC into `audio.bluetooth_address` in your **untracked** `config.json`. This ensures only that headset is preferred and enables periodic reconnection attempts.
4. Inspect `pactl -f json list cards` and `pactl -f json list sources`. WI-C100 must expose a **headset/handsfree microphone** profile/source. If A2DP playback works but `bluez_input.*` is absent, fix the Bluetooth HFP/HSP support/audio-session configuration first.
5. Run `python -m alfred --doctor`, then the continuous agent.

`audio.mode`:
- `auto` (default): WI-C100 HFP/HSP if available; otherwise approved non-Bluetooth source.
- `bluetooth`: WI-C100 microphone only; no silent fallback.
- `direct`: explicitly configured `direct_source`, or a detected ALSA/USB microphone through the audio server.
- `auto_fallback: false`: auto mode also refuses to fall back to direct.

Audio is captured from the **source**, not a sink/monitor. The service uses the system audio server for microphone access, not direct Bluetooth PCM protocols. Switching to HFP/HSP can lower playback fidelity compared to A2DP; this is normal.

For better noise rejection, tune `audio.speech_rms` using local speech and noise recordings. The initial RMS VAD is intentionally simple and configurable.

## Tool safety / behavior

Read-only: `get_system_status`, `get_gpu_status`, `list_services`, `list_reminders`, `list_schedules`, `list_workflows`.

Controlled: `control_service` only for `runtime.allowed_services`, `run_named_task` only for `runtime.named_tasks` (argv list, **no shell**). Services are limited to `systemctl --user` without privilege escalation.

Persistent: `create_reminder`, `cancel_reminder`, `schedule_every` (status checks only), `cancel_schedule`. Scheduled jobs are stored in SQLite WAL and recover across Alfred restarts, but are not an exactly-once distributed delivery system.

System 2: Qwen returns JSON plans. Python validates the tools, args, allowed conditions, max steps, and service/task allowlists; nested reasoning and recursive workflows are prohibited. Saving a named workflow requires an explicit "save/create workflow" voice request. Conditions may compare a **top-level field** from an earlier step, e.g. `gpu_temp_c > 80`. **Conditional recurring watches are not implemented**; recurring jobs currently run approved read-only diagnostic tools.

For safety and privacy, raw audio is not saved and the database logs **transcript length**, not content. Do not run Alfred with root privileges. Continuous listening is enabled by user choice; only use in spaces where recording is appropriate.

## Deployment as a service

Use `deployment/alfred.service` as a **user** service template (edit paths to your checkout). The audio daemon and BlueZ session must be available to that user; running `systemd --user` without a graphical/audio session may not be enough.

```bash
mkdir -p ~/.config/systemd/user
cp deployment/alfred.service ~/.config/systemd/user/alfred.service
systemctl --user daemon-reload
systemctl --user enable --now alfred
journalctl --user -u alfred -f
```

## Acceptance checklist on hardware

- [ ] WI-C100 pairs and its microphone source shows up in `pactl` with an HFP/HSP profile.
- [ ] `--doctor` prints the Bluetooth and direct sources.
- [ ] Direct mode functions with a USB/analog microphone.
- [ ] Auto mode switches Sony → direct on disconnect, direct → Sony on reconnect.
- [ ] `--ensure-llama` adopts an existing process or launches an absent one from the configured GGUF.
- [ ] `/v1/models` shows the intended Qwen model; `nvidia-smi` is generally not the standard Jetson monitoring method—use `tegrastats`.
- [ ] Cactus native engine loads Needle and Whistle, and streaming transcription produces words.
- [ ] Voice actions are routed correctly; nothing executes on pending text.
- [ ] Reminders and recurring schedules work after service restart.
- [ ] Continuous capture remains responsive during Qwen inference; measure P50/P95 latency and RAM.

Official references: [Needle/Whistle APIs](https://github.com/cactus-compute/needle/blob/main/llms.txt) · [llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

## Development state

This is a first hardware-integration-ready implementation with offline unit fixtures, **not a claim of on-device testing**. See the feature PR for follow-up fixes/Jetson acceptance.
