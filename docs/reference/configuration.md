# Configuration reference

Alfred reads one optional JSON file (default `config.json`, set with
[`--config`](cli.md)). Every key is optional. Missing keys take the defaults in
[`alfred/config.py`](../../alfred/config.py).

```bash
cp config.example.json config.json
```

## How configuration is loaded

1. Start from the built-in `DEFAULT` dictionary.
2. Merge the JSON file over it. **An unknown key at any level raises `ValueError`.**
   `runtime.named_tasks` is replaced as a whole instead of merged.
3. Apply [environment overrides](#environment-overrides).
4. Run `_validate()`.

If the file named by `--config` does not exist, Alfred silently uses the defaults.

```{note}
`config.example.json` is not identical to the built-in defaults. It sets
`qwen.model` to `bartowski/Qwen_Qwen3.5-4B-GGUF:Q4_K_M` (default: empty) and
`qwen.max_tokens` to 768 (default: 500). Copying the example therefore changes behaviour.
```

## What is validated, and when

| Check | Where | Runs on every start? |
|---|---|---|
| `qwen.backend` is a known value; `qwen.url` is a loopback HTTP origin with a port and no path | `config._validate` | Yes |
| `wake_word.*` types and ranges | `config._validate` | Yes |
| Every other range listed below | Pydantic models (`AudioConfig`, `QwenConfig`, `RuntimeConfig`) via `load_config_model()` | **No.** `cli.py` calls `load_config()`, not `load_config_model()` |
| Context TTL and budget, Needle loop limits, Harness timeout, plan step count | The component that consumes the value | Yes, when that component is built |

Values that only the Pydantic models check (for example `audio.frame_ms`, `audio.mode`,
`runtime.approval_ttl_seconds`) are **not** rejected at startup. Use `load_config_model()` in
your own tooling if you need the full check.

## `audio`

Controls microphone selection and utterance capture. See [Hardware setup](../getting-started/hardware-setup.md).

| Key | Default | Valid values | Meaning |
|---|---|---|---|
| `mode` | `"auto"` | `auto`, `direct`, `bluetooth` | `auto` prefers the Bluetooth headset, then a direct microphone. |
| `sony_name` | `"WI-C100"` | string | Substring matched against the Bluetooth device description. |
| `bluetooth_address` | `""` | MAC address or empty | Pin a specific headset. |
| `direct_source` | `""` | PulseAudio source name or empty | Pin a specific direct microphone. |
| `poll_seconds` | `5` | int | Interval between audio health checks. |
| `frame_ms` | `500` | `250`, `500`, `1000` | PCM frame length. |
| `speech_rms` | `0.013` | 0.001 to 0.2 | Energy threshold that counts as speech. |
| `end_silence_frames` | `2` | int | Silent frames that end an utterance. |
| `max_utterance_seconds` | `25` | int | Hard cap on one utterance. |
| `auto_fallback` | `true` | bool | In `auto` mode, fall back to direct sources. |
| `probe_timeout_seconds` | `5` | 1 to 30 | Timeout for the live-PCM probe. |
| `read_timeout_seconds` | `5` | 1 to 30 | Timeout for one frame read. |

## `wake_word`

See the [Wake-word guide](../guides/wake-word.md).

| Key | Default | Valid values | Meaning |
|---|---|---|---|
| `enabled` | `true` | bool (strict) | When false, every transcript is processed. |
| `phrase` | `"Alfred"` | 1 to 40 characters, starts with a letter, then letters, digits or spaces; no leading or trailing space | Phrase matched in the transcript. |
| `mode` | `"transcript"` | `transcript`, `openwakeword` | Detector. `openwakeword` gates audio before Whistle runs. |
| `followup_seconds` | `8` | 0 to 30 | After a wake, further utterances are accepted for this long. |
| `model_path` | `""` | string | Local ONNX model, used by `openwakeword`. |
| `threshold` | `0.75` | greater than 0, at most 1 | Detector score needed to wake. |

## `qwen`

The local planner service. See [Inference backends](../guides/inference-backends.md).

| Key | Default | Valid values | Meaning |
|---|---|---|---|
| `backend` | `"auto"` | `auto`, `llama_cpp`, `llama_server`, `ollama`, `lm_studio` | `-` and `.` are normalised to `_`; `lmstudio` and `lms` map to `lm_studio`. |
| `url` | derived | `http://127.0.0.1:PORT`, `localhost` or `::1`, explicit port, no credentials, no path | See [URL resolution](#qwen-url-resolution). |
| `binary` | `""` | path or name | Backend executable. Empty means the backend's default name. |
| `model` | `""` | string | Model alias to request. Empty falls back to `expected_model`. |
| `model_path` | `""` | path | GGUF file for `llama_cpp` and `llama_server`. |
| `expected_model` | `"Qwen3.5-4B"` | string | Alias an adopted server must expose. |
| `ctx_size` | `4096` | 1 to 131072 | Context window passed to llama.cpp. |
| `gpu_layers` | `99` | int | GPU layers passed to llama.cpp. |
| `startup_timeout_seconds` | `120` | int | How long to wait for the server to become ready. |
| `max_tokens` | `500` | int | Completion limit. Requests to the explain endpoint use `min(512, max_tokens)`. |
| `enabled` | `true` | bool | When false, the watchdog does not start and `--reason` reports a disabled completion. Voice and `--text` modes refuse to start, because the Harness requires Qwen. |
| `supervisor_poll_seconds` | `30` | 10 to 3600 | Watchdog interval. |

Default executables and ports:

| Backend | Executable | Port |
|---|---|---|
| `llama_cpp` | `llama` | 8080 |
| `llama_server` | `llama-server` | 8080 |
| `ollama` | `ollama` | 11434 |
| `lm_studio` | `lms` | 1234 |

With `backend: "auto"`, the backend is inferred from the executable name, then from port 11434
(Ollama) or 1234 (LM Studio), and otherwise defaults to `llama_cpp`.

### Qwen URL resolution

`qwen.url` is, in order: `ALFRED_QWEN_URL`, `ALFRED_LLAMA_URL`, the `qwen.url` key in the JSON
file, or `http://127.0.0.1:<default port of the selected backend>`. A trailing `/` is removed.
Pass the server origin only, without `/v1`.

## `runtime`

### Storage and policy

| Key | Default | Valid values | Meaning |
|---|---|---|---|
| `database` | `~/.local/share/alfred/alfred.db` | path | SQLite file. See [Data model](../architecture/data-model.md). |
| `logs` | `~/.local/state/alfred` | path | Log directory and shutdown sockets. |
| `allowed_services` | `[]` | list of systemd unit names | The only services `list_services` and `control_service` may touch. |
| `named_tasks` | `{}` | object mapping a task name to an argv list of strings | The only commands `run_named_task` may run. Executed without a shell, 60 s timeout. |
| `max_plan_steps` | `5` | 1 to 10 | Longest plan accepted from Qwen. |
| `approval_ttl_seconds` | `180` | 30 to 3600 | Lifetime of a pending approval. |
| `tts` | `false` | bool | Speak replies with `spd-say` if it is installed. |
| `voice_alerts` | `false` | bool | Accepted but not read by any module in this version. |
| `telemetry` | `false` | bool | When false, Alfred sets `NEEDLE_TELEMETRY=0` unless already set. |

### Routing and cognition

| Key | Default | Valid values | Meaning |
|---|---|---|---|
| `needle_confidence_floor` | `0.7` | float | Needle results below this confidence are not trusted. |
| `needle_loop_enabled` | `true` | bool | Enables the multi-step [Needle loop](../architecture/cognitive.md). |
| `needle_max_decisions` | `3` | 1 to 3 | Needle decisions per request. |
| `needle_max_tools` | `3` | 1 to 3 | Needle tool calls per request. |
| `escalation_enabled` | `true` | bool | Allow handing off from Needle to Qwen. |
| `harness_timeout_seconds` | `45` | 5 to 120 | Timeout for one DeepSeek Harness turn. |
| `cognitive_diagnostics` | `false` | bool | Record `Diagnostic` events. Skipped when admission is busy. |

### Context

| Key | Default | Valid values | Meaning |
|---|---|---|---|
| `context_enabled` | `true` | bool | Enables the bounded context packet. |
| `context_persistence_enabled` | `false` | bool | Allows explicit facts to be stored. Requires `context_enabled`. |
| `context_owner` | `"local"` | letter, then up to 31 letters, digits, `_` or `-` | Scope owner. |
| `context_mode` | `"assistant"` | `assistant`, `meeting` | Scope mode. |
| `context_ttl_seconds` | `120` | 1 to 300 | Lifetime of context slots. |
| `needle_context_ttl_seconds` | `120` | 1 to 300 | Lifetime of Needle session slots. |
| `context_budget_chars` | `1200` | 256 to 3200 | Hard size limit of the compacted context JSON. |

### Resource admission

See [Concurrency](../architecture/concurrency.md#inference-admission).

| Key | Default | Meaning |
|---|---|---|
| `resource_admission_enabled` | `true` | Master switch. |
| `resource_min_available_mib` | `512` | Refuse when available RAM is below this. |
| `resource_max_swap_used_mib` | `1024` | Refuse when swap in use is above this. |
| `resource_max_cpu_load_per_core` | `3.0` | Refuse when load per core is above this (not applied to Whistle). |
| `resource_max_wait_ms` | `200` | How long a request waits for its turn. |

## Environment overrides

Environment variables win over the JSON file.

| Variable | Overrides | Notes |
|---|---|---|
| `ALFRED_QWEN_BACKEND` | `qwen.backend` | Normalised as described above. |
| `ALFRED_QWEN_BINARY` | `qwen.binary` | Takes precedence over `ALFRED_LLAMA_SERVER`. |
| `ALFRED_QWEN_MODEL` | `qwen.model` | |
| `ALFRED_QWEN_URL` | `qwen.url` | Takes precedence over `ALFRED_LLAMA_URL`. |
| `ALFRED_GGUF` | `qwen.model_path` | Legacy name. |
| `ALFRED_LLAMA_SERVER` | `qwen.binary` | Legacy name. |
| `ALFRED_LLAMA_URL` | `qwen.url` | Legacy name. |
| `NEEDLE_TELEMETRY` | n/a | Alfred sets it to `0` when `runtime.telemetry` is false. |

## Minimal example

```json
{
  "qwen": { "backend": "ollama", "model": "qwen3.5:4b" },
  "runtime": {
    "allowed_services": ["alfred-scheduler.service"],
    "named_tasks": {}
  }
}
```
