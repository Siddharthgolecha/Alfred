# Troubleshooting

Symptoms, causes and fixes. Messages quoted here come from the source. For setup steps see
[Hardware setup](../getting-started/hardware-setup.md) and
[Inference backends](inference-backends.md).

Start with:

```bash
alfred --config config.json --doctor
echo $?
```

## Microphone

### `No usable microphone detected` (exit status 2)

Alfred found no eligible recording source, so voice mode stops before loading any model. Causes:

- The headset is not connected, or exposes only A2DP (playback) and no HFP/HSP microphone.
- Only a generic **Built-in Audio** source exists. Alfred rejects it because a label is not proof
  that a microphone is attached.
- Every source is a `.monitor` (playback loopback), which is never used.

Fix:

1. Run `--doctor`. Each source is `[CANDIDATE]` or `[REJECTED]` with a reason.
2. Connect the headset, or attach a USB or analog microphone.
3. If a real microphone is wired to onboard audio, set `audio.direct_source` to its name from
   `pactl -f json list sources`.
4. Restart the service: `systemctl --user restart alfred`.

### `No usable microphone delivered PCM`

A source was listed but no audio bytes arrived within `audio.probe_timeout_seconds` (default 5).
Check the Bluetooth HFP profile, the input port, and permissions on the audio server's socket.
Silence on a working stream is fine, but zero bytes is a failure.

### `Cannot launch parec ... install pulseaudio-utils`

```bash
sudo apt install -y pulseaudio-utils
```

### `Cannot enumerate audio inputs`

The audio server is not reachable. Verify PipeWire or PulseAudio is running and that your session
can reach its socket.

### Audio drops while listening

Alfred tries another candidate. After three consecutive recovery failures it exits with status 2
instead of retrying forever. For a Bluetooth headset with `audio.bluetooth_address` set, it runs
`bluetoothctl connect` at most once every 30 s.

### Speech is missed, or noise triggers capture

Tune `audio.speech_rms`. See [Hardware setup](../getting-started/hardware-setup.md#tune-speech-detection).

## Wake word

| Symptom | Cause and fix |
|---|---|
| Alfred ignores your command | The phrase must start the utterance. Say "Alfred, ..." or "Hey Alfred, ...". |
| Follow-up not accepted | The window is `wake_word.followup_seconds` (default 8) and covers one utterance. |
| `wake_word.mode=openwakeword requires a local custom model` | Set `wake_word.model_path` to an existing ONNX file. |
| `Acoustic wake detection requires: pip install '.[wakeword]'` | Install the extra: `pip install -e '.[wakeword]'`. |
| Everything is processed, no wake word needed | `wake_word.enabled` is `false`. |

See [Wake word](wake-word.md).

## Inference backend and planner

### `... is occupied by another/unhealthy service; not replacing it`

Something else holds the configured port. Alfred never replaces it. Stop that service, or set
`qwen.url` to another loopback port.

### `Harness NOT READY: Qwen model/endpoint not available`

`--doctor` could not reach a ready model. Run `--ensure-backend` and read its JSON. `health`
should be `ready`. Check that `qwen.enabled` is true, because the Harness needs Qwen.

### `Harness NOT READY: ...` with a version or plugin message

The pinned DeepSeek Harness packages are missing, the wrong version (`0.1.5rc1` is required), or
the effective plugin set differs from the manifest. Reinstall with
`scripts/install_harness.py`. See [Installation](../getting-started/installation.md#offline-or-checksum-verified-harness-install).
There is no fallback to another planner.

### The model is not found

- Ollama: install it first with `ollama pull <name>`. Alfred never downloads Ollama models.
- LM Studio: download the model first and make sure `lms` is installed.
- Any backend: `qwen.model` must match an ID the server reports at `/v1/models`, or
  `qwen.expected_model` must match exactly one.

### The planner returns no action

Alfred asks llama backends for non-thinking output and retries a malformed plan once, generation
only. Then it reports that no action was taken. It never claims a schedule was saved.

```bash
alfred --config config.json --reason "Schedule a GPU status check every 10 minutes"
```

If it fails repeatedly, check the chat template, the backend logs and `qwen.max_tokens`. A vague
request needs both an action and an interval.

### Qwen is slow, or requests are refused as busy

Resource admission can refuse or delay a model request when memory is short or the CPU is busy.
Tune `runtime.resource_*` or free memory. See
[Concurrency](../architecture/concurrency.md#inference-admission).

## Configuration

| Message | Fix |
|---|---|
| `ValueError` naming an unknown key | A key in `config.json` is not recognised, at any level. Check spelling against the [reference](../reference/configuration.md). |
| `qwen.url` rejected | Use a loopback HTTP origin with an explicit port, no path (no `/v1`) and no credentials. |
| A removed `runtime.harness_enabled` flag | It is obsolete. Delete it. |
| Config file seems ignored | If the path given to `--config` does not exist, Alfred silently uses the defaults. Check the path. |

## Approvals and workflows

| Message | Meaning |
|---|---|
| `Approval missing, expired, or already acted on` | The ID is wrong, the TTL (`runtime.approval_ttl_seconds`) ran out, or it was already approved or rejected. Ask again to create a new one. |
| `Workflow changed since voice approval request` | The graph was edited after the approval was staged. Request the run again. |
| `Workflow missing or not locally approved` | Run `--workflow-approve NAME` first. |
| A run is stuck in `running` after a crash | Inspect for side effects, then `--workflow-reconcile RUN_ID`. |

## Startup and shutdown

| Message | Fix |
|---|---|
| `Alfred voice is already running` (or `scheduler`) | One instance per role. Run `alfred close`, then start again. |
| `Alfred is not running` | Nothing to close. |
| `context and context persistence must both be opted into` | `--memory-*` needs `runtime.context_enabled` and `runtime.context_persistence_enabled` both true. |
| Service will not restart after a missing microphone | Intended. `RestartPreventExitStatus=2` stops the loop. Fix the microphone, then restart. |

## Still stuck

Collect `--doctor` output and `journalctl --user -u alfred`, then open an issue. Remove addresses
and personal text first. See [`SECURITY.md`](https://github.com/Siddharthgolecha/Alfred/blob/main/SECURITY.md) for security problems.
