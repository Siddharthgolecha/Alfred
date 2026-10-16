# Testing

Alfred's tests run offline. They need no Jetson, microphone, GGUF model or native Cactus engine.
CI uses the standard-library `unittest` runner, so that is the runner these docs use.

## Run the tests

```bash
python -m compileall -q alfred tests       # syntax check
python -m unittest discover -s tests -v    # full suite
python -m unittest tests.test_wakeword     # one module
```

The full suite takes several minutes on a laptop. A few tests start real subprocesses and local
HTTP servers.

:::{note}
Three tests in `tests/test_alfred.py` (`ModelTests`) currently error with
`module 'alfred.models' has no attribute 'shutil'`. They patch `alfred.models.shutil`, which no
longer exists. This is also true of a clean checkout of `HEAD`, so it is not caused by
documentation changes. It is listed under "Known issues" in the
[changelog](https://github.com/Siddharthgolecha/Alfred/blob/main/CHANGELOG.md#unreleased).
:::

## What the tests cover

| Area | Files |
|---|---|
| Voice path and routing | `test_voice_routing.py`, `test_router.py`, `test_listener.py`, `test_wakeword.py`, `test_audio_failfast.py` |
| Planning and approval | `test_system2.py`, `test_escalation.py`, `test_needle_loop.py` |
| Cognitive layer | `test_cognitive.py`, `test_context.py`, `test_session.py` |
| Harness | `test_harness_required.py`, `test_harness_sdk.py`, `test_harness_bridge.py` |
| Backends and lifecycle | `test_backends.py`, `test_shutdown.py`, `test_inference_admission.py` |
| Automations and workflows | `test_automations.py`, `test_phase3.py`, `test_phase4.py` |
| Configuration and validation | `test_config.py`, `test_config_models.py`, `test_constants.py`, `test_validators.py`, `test_pydantic_models.py` |
| Skills and meetings | `test_skills_meeting.py` |
| Release gates | `test_phase5_release.py` |
| CI and branch sync | `test_branch_sync.py` |
| Runtime and app | `test_runtime.py`, `test_alfred.py` |

When you change behaviour, add or update a test in the matching file. Test the negative cases for
anything that touches approvals, shell or network access, persistence or replay.

## Benchmarks and QA scripts

These live in `tests/` but are scripts, not unit tests. Run them from the repository root.

| Script | Purpose |
|---|---|
| `bench_needle_fastpath.py` | P50/P95 of the Needle fast path with mocked inference and a paired baseline. |
| `bench_handoff.py` | Same for the Needle-to-Qwen handoff. |
| `bench_context_fastpath.py` | Overhead of scoped context. |
| `phase5_qa.py` | Offline policy matrix: 12 scenario families, 12 trials each, 3 modes (432 rows). Writes `fixtures.json`, `raw.csv`, `report.json` and `report.md` to `--out`. |
| `sdk_smoke.py` | Starts the genuine pinned Cordis runtime against a local stub model. Needs the Harness wheels installed. |
| `sdk_live_qwen_probe.py` | Opt-in probe with a **real** local model. Takes `--url` and `--model`. |

```bash
PYTHONPATH=. python tests/bench_needle_fastpath.py
PYTHONPATH=.:tests python tests/phase5_qa.py --out artifacts/phase5-offline
PYTHONPATH=. python tests/sdk_live_qwen_probe.py --url http://127.0.0.1:8080 --model <id>
```

## Continuous integration

[`.github/workflows/check.yml`](../../.github/workflows/check.yml) runs on pushes to `main` and
`feat/jetson-voice-agent`, on pull requests, and on manual dispatch.

| Job | Runs on | What it does |
|---|---|---|
| `python` | Python 3.10 and 3.12 | `compileall`, the unit suite, the three benchmarks, the Phase 5 matrix (uploaded as an artifact), then the context benchmark. |
| `harness-sdk-smoke` | Python 3.12 | Downloads the pinned SDK and runtime wheels, checks their SHA-256 against `coordination/deepseek-harness-lock.json`, checks that the built Alfred wheel carries the Cordis overlay, then runs `sdk_smoke.py`. |
| `review-base-to-head` | Pull requests only | Exports the exact base-to-head diff as an artifact for independent review. |

A separate workflow merges `main` into `intrinsic_jetson`. See
[Maestaris workflow](maestaris.md).

## What a green build does not prove

CI cannot establish:

- Bluetooth profile support on your JetPack image,
- the native Cactus ARM64 binaries, CUDA behaviour, or real memory and latency,
- spoken feedback,
- that the Phase 5 matrix reflects real Jetson performance. Its models, telemetry and devices
  are mocks, and every record carries `native_jetson=false`.

Treat a Jetson as unvalidated until the
[hardware checklist](../getting-started/hardware-setup.md#hardware-acceptance-checklist) and the
[release process](release-process.md) are complete.
