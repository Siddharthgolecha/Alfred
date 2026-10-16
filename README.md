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

Requires Python 3.10+, BlueZ, and a PulseAudio-compatible audio server on the board.

```bash
git clone https://github.com/Siddharthgolecha/Alfred.git
cd Alfred
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
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
