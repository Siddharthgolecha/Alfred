# Inference backends

Alfred plans complex requests with a local Qwen 3.5 model. It talks to the model through an
OpenAI-compatible server on loopback. Four servers are supported. Alfred adopts a compatible
server that is already running, or starts the one you configure.

For the options see [Configuration: `qwen`](../reference/configuration.md#qwen). For how Alfred
uses the model see [Cognitive layer](../architecture/cognitive.md).

## Choosing a backend

| `qwen.backend` | Server | Default port | Default executable |
|---|---|---|---|
| `llama_cpp` | `llama serve` | 8080 | `llama` |
| `llama_server` | `llama-server` (legacy llama.cpp) | 8080 | `llama-server` |
| `ollama` | Ollama | 11434 | `ollama` |
| `lm_studio` | LM Studio | 1234 | `lms` |
| `auto` (default) | Inferred from `qwen.binary`, then from port 11434 or 1234, else `llama_cpp` | | |

The URL must be a loopback HTTP origin with an explicit port and no path (do not add `/v1`).
Leave `qwen.url` empty to use the port above.

## Configuration examples

Only the `qwen` keys that differ from the defaults are shown.

### llama.cpp (`llama serve`)

```json
{ "qwen": { "backend": "llama_cpp", "binary": "llama",
            "model": "bartowski/Qwen_Qwen3.5-4B-GGUF:Q4_K_M" } }
```

`model` is passed as `-hf <repo:quant>`. The model is downloaded and cached on the first start,
and an existing cache is reused. To run strictly offline, use `model_path` instead.

### llama-server with a local GGUF

```json
{ "qwen": { "backend": "llama_server",
            "binary": "/path/to/llama.cpp/build/bin/llama-server",
            "model_path": "/path/to/model.gguf" } }
```

`qwen.binary` may be an executable, a name on `PATH`, or a llama.cpp directory. For a directory
Alfred searches `build/bin/` and the directory itself. Symlinks into the Hugging Face cache work
for `model_path`.

### Ollama

```json
{ "qwen": { "backend": "ollama", "model": "qwen3.5:4b" } }
```

The model must already be installed (`ollama pull qwen3.5:4b`). Alfred never downloads an Ollama
model or substitutes another one.

### LM Studio

```json
{ "qwen": { "backend": "lm_studio", "model": "your-loaded-model-id" } }
```

Requires the `lms` CLI and a previously downloaded model.

## What Alfred does at start

`alfred --ensure-backend` (alias `--ensure-llama`) runs the same logic as voice mode, prints the
status as JSON and exits.

```{mermaid}
flowchart TD
    start["Start / --ensure-backend"] --> port{"Is something on<br/>the configured port?"}
    port -- "healthy, compatible" --> adopt["Adopt it<br/>(managed = false)"]
    port -- "unhealthy or another service" --> fail["RuntimeError:<br/>not replacing it"]
    port -- "nothing" --> spawn["Start the backend<br/>(managed = true)"]
    adopt --> model{"Requested model<br/>served?"}
    spawn --> model
    model -- yes --> ready["Ready"]
    model -- no --> fail2["Fail: model not verified"]
```

How each backend is started:

| Backend | Command |
|---|---|
| `llama_cpp` | `llama serve --model <file>` or `-hf <repo:quant>`, `--host 127.0.0.1`, `--port`, `--ctx-size`, `-ngl`, and one parallel slot |
| `llama_server` | The same arguments on the `llama-server` executable |
| `ollama` | `ollama serve` with `OLLAMA_HOST=127.0.0.1:<port>` |
| `lm_studio` | `lms server start --port <port> --bind 127.0.0.1`, then `lms load <model> --yes` |

The context size and GPU layers come from `qwen.ctx_size` and `qwen.gpu_layers`. They apply to
the llama.cpp backends only. Ollama and LM Studio manage their own runtime settings, and Alfred
never changes the memory settings or loaded models of a server it adopts.

### Model selection

1. If `qwen.model` is set and the server serves a matching ID, use it.
2. A single served llama model can match through `qwen.expected_model`.
3. Otherwise `qwen.expected_model` must match exactly one served model.

`qwen.expected_model` only filters the automatic choice when `qwen.model` is empty.

### Status

The JSON printed by `--ensure-backend` has these keys:

| Key | Meaning |
|---|---|
| `backend` | Resolved backend name. |
| `url` | Server origin. |
| `health` | `ready`, `loading`, `unhealthy` or `down`. |
| `pid` | Process ID of a server Alfred started. |
| `managed` | `true` if Alfred started the server. |
| `model` | Model ID in use. |

## Stopping

On shutdown Alfred stops **only a server it started**. It sends SIGTERM to the process group,
then SIGKILL if that times out. For LM Studio it runs `lms server stop`. A server that Alfred
adopted is never stopped. `--ensure-backend` leaves its server running by design, and a later
Alfred session does not own it.

A watchdog polls every `qwen.supervisor_poll_seconds` (default 30).

## Troubleshooting the planner

Alfred asks llama backends for non-thinking output, because some Qwen 3.5 chat templates spend the
whole output budget on thinking and return an incomplete plan. A malformed plan gets one retry
that regenerates only. If it is still invalid, Alfred reports that no action was taken. It never
claims a schedule was saved and never runs a partly parsed step.

Test the endpoint:

```bash
alfred --reason "Schedule a GPU status check every 10 minutes"
```

If it fails repeatedly:

1. Check that the model and its chat template are compatible with non-thinking JSON output.
2. Read the backend's own logs.
3. Check `qwen.max_tokens`. `config.example.json` sets 768, and the built-in default is 500.

A vague request such as "create a schedule" needs an action and an interval. Sensitive schedules
are staged and need `--approve ID`.

More fixes: [Troubleshooting](troubleshooting.md).
