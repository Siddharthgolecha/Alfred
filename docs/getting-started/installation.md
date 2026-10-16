# Installation

Install Alfred on Linux, macOS, Windows x64 or Jetson Orin Nano, then connect it to a local inference backend.

:::{note}
Alfred is unit-tested offline but has not been validated on Jetson hardware. Treat the steps
below as the intended procedure and verify each one on your board. See the
[hardware checklist](hardware-setup.md#hardware-acceptance-checklist).
:::

## Scripted installation

```bash
git clone https://github.com/Siddharthgolecha/Alfred.git
cd Alfred
./install.sh
```

On Windows, use PowerShell from the cloned project directory:

```powershell
.\install.ps1
```

The guided terminal flow is compact and keeps its answers in the terminal history:

```text
▲  Alfred  guided setup
› System check                 Linux aarch64 · Python 3.12
› Microphone
  ❯ Use the detected default
    Configure later
› Choose your local model server
  ❯ Ollama · detected
    LM Studio
    llama.cpp
    Configure later
Setup review
  Microphone   auto
  Model server ollama
  Model        qwen3.5:4b
  Model pulls  offer if needed
› Continue with these settings?  Yes
```

Use arrow keys and Enter in a compatible terminal, or pass `--plain` for numbered prompts.
On an existing installation, choose **Quick setup** to reuse saved choices or **Customize**
to review them again. Alfred saves completed installation steps in your user state directory
and rechecks each step before reusing it. A rerun with an unchanged environment reuses the
verified Harness wheels and installed packages. If the virtual environment is damaged, use
`--repair` to move it to `.venv.recovery/` before Alfred builds a replacement.

The installer uses Python's standard library for its terminal interface. It guides first-time
setup, checks the selected platform, and verifies the pinned Harness wheels before installation.
When LM Studio is selected with a loopback URL, the installer starts its local API server on the
configured port if it is not already running. It does not download or load a model implicitly;
the setup summary reports when the configured model is not available from that server.
Windows voice capture requires FFmpeg with DirectShow; Alfred offers `winget` setup when it is
available. Allow microphone access in Windows Privacy settings. Installers do not claim voice
readiness until `alfred --doctor` verifies a live microphone stream.

| Flag | Purpose |
|---|---|
| *(none)* | Guided interactive setup |
| `--auto` | Non-interactive / CI / headless |
| `--service` | Also register a systemd auto-start service |
| `--backend ollama` | Use Ollama (default) |
| `--backend llamacpp` | Configure a local llama.cpp server or GGUF model |
| `--skip-models` | Skip model cache download (air-gapped) |
| `--no-update-check` | Compatibility flag; the installer does not run update checks |
| `--wheelhouse PATH` | Require local Harness wheels (verified; no network fallback) |
| `--plain` | Use numbered line prompts |
| `--customize` | Reopen setup choices on an existing install |
| `--repair` | Preserve and rebuild a damaged `.venv` |

:::{note}
`config.json` is optional — Alfred runs on built-in defaults without it. The wizard creates or safely updates it when you choose a microphone or inference backend.
:::

## Requirements

| Requirement | Detail |
|---|---|
| Python | 3.10 or newer for Alfred core. CI runs 3.10 and 3.12. For Linux acoustic wake-word installs on Python 3.12+, use `uv sync` as described below. |
| Bluetooth | A working adapter with BlueZ, if you use a Bluetooth headset. |
| Audio server | PulseAudio, or PipeWire with `pipewire-pulse`. Alfred records with `parec` and inspects devices with `pactl`. |
| Inference backend | One of llama.cpp, Ollama or LM Studio, serving a Qwen 3.5 model on loopback. See [Inference backends](../guides/inference-backends.md). |
| Operating system user | A normal user. Never run Alfred as root. |

## Manual installation

Use these steps if you prefer to set up Alfred without the install script, or to understand what the script does.

### 1. System packages

```bash
sudo apt update
sudo apt install -y bluez pulseaudio-utils libasound2-dev python3-venv
```

### 2. Python environment

```bash
git clone https://github.com/Siddharthgolecha/Alfred.git
cd Alfred
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

`pip install -e .` also installs the pinned DeepSeek Harness SDK and runtime
(`0.1.5rc1`), `cactus-needle` and `pydantic`. The Harness is required: interactive Alfred does
not start without it. See [DeepSeek Harness](../architecture/harness.md).

Optional extras:

| Extra | Command | Purpose |
|---|---|---|
| `wakeword` | `uv sync --extra wakeword` (Linux, recommended), or `pip install -e '.[wakeword]'` (Python 3.10/3.11 only) | `openwakeword` and `numpy`, for [acoustic wake-word gating](../guides/wake-word.md#acoustic-gating). |
| `test` | `pip install -e '.[test]'` | `pytest`, an optional runner. CI and these docs use `python -m unittest`. |
| `docs` | `pip install -e '.[docs]'` | Sphinx toolchain, to [build these docs](../development/documentation.md). |

### Acoustic wake-word dependency on Linux (Python 3.12+)

Alfred selects openWakeWord's **ONNX** inference engine, so it does not use TensorFlow Lite.
Upstream `openwakeword==0.6.0` nevertheless declares `tflite-runtime` on Linux, and
`tflite-runtime==2.14.0` publishes wheels only for CPython 3.10 and 3.11.
On Python 3.12+, `pip install -e '.[wakeword]'` can therefore fail even though ONNX works.
The repository's `[tool.uv]` setting deliberately excludes this unused transitive package
from **uv project syncs**; it does not change how `pip` resolves requirements.

For the project's default `.venv`:

```bash
uv sync --extra wakeword
```

For an already activated environment outside the project (for example
`/home/intrinsic/.venvs/alfred/`), explicitly target it:

```bash
uv sync --active --extra wakeword
```

Without `--active`, uv uses the project's `.venv` and warns that the other
`VIRTUAL_ENV` is being ignored. Make sure `python -V` and `uv run python -V`
refer to the environment you intended. For Python 3.10/3.11, the `pip` command
in the optional-extras table remains available.

#### Offline or checksum-verified Harness install

`pip install` cannot verify the Harness wheels against the published hashes after the fact.
To verify first, download the pinned wheels into a directory and use the installer. The expected
file names and SHA-256 values are in
[`coordination/deepseek-harness-lock.json`](../../coordination/deepseek-harness-lock.json).

```bash
# Check the hashes only
python scripts/install_harness.py --wheelhouse /path/to/wheels --verify

# Check, then install with pip --no-index --no-deps
python scripts/install_harness.py --wheelhouse /path/to/wheels --install
```

Pass `--platform linux_x86_64`, `linux_aarch64`, `macos_x86_64`, `macos_aarch64`, or
`windows_x86_64` to choose the runtime wheel. By default it is detected from the machine.
`--install` refuses a wheel for another platform. The
script exits with status 2 and prints `Harness wheelhouse verification failed` on any error.

### 3. Configuration

```bash
cp config.example.json config.json
```

Keep `config.json` out of version control. It may hold a Bluetooth address and local paths. Every
key is optional; see the [configuration reference](../reference/configuration.md).

:::{warning}
`config.example.json` is not identical to the built-in defaults. It selects the Bartowski
Qwen model and a larger `max_tokens`. Read it before relying on it.
:::

### 4. Model caches

Populate the Cactus caches while you still have network access:

```bash
needle download needle3
needle download whistle
```

The Qwen model comes from your inference backend. For llama.cpp's `-hf` form, the first start
downloads and caches it. To run strictly offline, set `qwen.model_path` to a local GGUF file
instead.

### 5. Build llama.cpp (optional)

If you use llama.cpp on the Orin with CUDA, a typical native build is:

```bash
cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87
cmake --build llama.cpp/build --config Release -j4
```

Set `qwen.binary` to the `llama.cpp` directory or to `build/bin/llama-server`. Start with a
context of 4096 and parallelism 1, and lower `qwen.gpu_layers` if the shared 8 GB of RAM
becomes constrained. Alfred does not change the memory settings of a server that is already
running.

### 6. Verify

```bash
python -m unittest discover -s tests -v    # no hardware needed
alfred --doctor
```

`--doctor` exits with status 2 if there is no usable microphone or if the Harness audit fails.

Next: [Quickstart](quickstart.md).
