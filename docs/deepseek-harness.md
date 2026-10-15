# DeepSeek Harness: required-runtime foundation (Issue #58 / PR #59)

DeepSeek Harness SDK and bundled Cordis are **required** for Qwen planning.
Install with `python -m pip install -e .`. The Python packages are pinned
to `deepseek-harness-sdk==0.1.5rc1` and
`deepseek-harness-runtime-bin==0.1.5rc1`. The runtime is local-only;
the SDK connects to the existing Qwen loopback backend selected from
llama.cpp / llama-server / Ollama / LM Studio. The SDK does not own that
model server. No cloud credential, provider or network is permitted.

## Actual feature boundary

This PR is the **P0-A foundation**, not the complete Issue #58: native Needle
and its bounded feedback still operate in Alfred Python, not the Cordis
session event loop. The validated `sdk-minimal` Cordis profile has an **empty
model-facing tool registry**. A future PR must implement a genuine typed
Cordis tool/observation round trip and bring Needle decisions into the
same Cordis task/session lifecycle. Native Jetson release gates #18 and #29
are still unverified.

Cordis is permitted to **propose** untrusted, JSON typed tool steps; proposals
do not execute or approve any action. Only Alfred `System2.parse_plan` /
`execute_or_stage` may validate an allowlisted proposal and decide whether to
execute read-only operations or await a separate one-shot local CLI approval.
A returned model string is never accepted as proof a step executed. The
effective `DSH_SYSTEM_PROMPT` explicitly permits *proposals*, but forbids
Cordis shell/filesystem/network/plugin execution, approval or fabricated results.

## Mandatory default and admission

Fresh installations enable scoped session context, native Needle feedback,
bounded escalation and inference admission. `runtime.harness_enabled` was
removed; including that obsolete key is a configuration error. Previously
supported SDK->direct-Qwen planner fallbacks are intentionally removed.
Missing, wrong-version or unsafe SDK/Cordis stops interactive startup.
`--doctor` checks the installed SDK/runtime and effective Cordis graph,
actual owned SDK subprocess startup, Qwen endpoint/model availability, and
microphone independently. The doctor does not assert a functional typed
tool bridge.

All expensive subprocess startups use serial, *non-nested* resource admission:
Cordis `--dump-config` executable audit claims `harness`, Qwen cold start
claims `qwen`, and owned Cordis SDK startup claims `harness`. Inference
requests separately claim their own slot. Critical resource pressure and
active microphone capture may defer/refuse a new expensive startup. A gate
cannot preempt an already running model. `--approve`, reminders, timers and
durable workflow execution retain their own local policy path.

## Trusted packages and offline install

Upstream: https://github.com/deepseek-ai/deepseek-harness
Pinned source audit reference: `571902028a75c64c1d767f4ae5fd099485f0ddf0`
(not a claim that published wheels were built from that Git SHA).
Official indices:
https://pypi.org/project/deepseek-harness-sdk/0.1.5rc1/
https://pypi.org/project/deepseek-harness-runtime-bin/0.1.5rc1/

Wheel SHA-256 checksums in `coordination/deepseek-harness-lock.json`:

- SDK universal wheel: `8d9897e97e8a95fd738cb2c3e2c563951d824e43847ec6f3b0fb172c0729b4d8`
- x86_64 Linux runtime: `ad877237382baff4628969a9ebdbfe38486d971b7987c032d0e29775759b5229`
- aarch64 Linux runtime: `32b7bb7887ea89f12462274d1b21acfafb00d38d94e11a5d7122492f185e4f85`

Offline installer for Linux x64/ARM64 (glibc >=2.28):

```bash
python scripts/install_harness.py --wheelhouse /path/to/wheels --verify
python scripts/install_harness.py --wheelhouse /path/to/wheels --install
```

The installer verifies wheel hashes before installing the two pinned wheels
with `--no-index --no-deps`; provision other Python dependencies from your
trusted offline cache. Installed distribution versions and actual plugin
composition are checked again at runtime. **A pip installation cannot be
retroactively verified against the exact published wheel hashes**; verification
must occur before installation or by preserving verified wheel artifacts.
The effective Cordis `--dump-config` is audited after applying
`alfred/cognitive/alfred_safe.patch.yml`. The default `sdk-minimal`
profile would enable dangerous shell/terminal services without that patch.

The SDK uses an isolated `0700` temporary workspace/home, which may contain
temporary session JSONL until normal cleanup. This does **not** guarantee secure
erasure after sudden loss of power or SIGKILL. Use disk encryption if needed;
do not claim an entirely memory-only pipeline.

## Verification and limits

Portable Python 3.10/3.12 tests cover approved-tool policy, non-replay,
admission under critical RAM/swap/CPU/capture, exact model identity and
shutdown. `tests/sdk_smoke.py` boots the **genuine pinned SDK/Cordis process**
using a deterministic local HTTP stub: it verifies effective plugin inventory,
prompt contract on the emitted OpenAI-compatible wire, JSON response shape and
lack of model-facing tools. Because that server **returns canned JSON**, it
cannot establish real Qwen's planning quality. The separate opt-in
`tests/sdk_live_qwen_probe.py` requires a **real locally running Qwen**
and checks a read-only typed plan is produced without executing it:

```bash
python tests/sdk_live_qwen_probe.py --url http://127.0.0.1:8080 \
    --model <exact-advertised-Qwen-model-id>
```

Real Qwen planning, multi-microphone speech and Orin Nano 8GB benchmarks
remain **unverified** until native results are attached. Offline tests do
not qualify native hardware; the SDK smoke has an x86_64 process only and
checks the published aarch64 wheel SHA-256 without running it on ARM64.

## Migration from Phase 5 opt-in preview

Remove `runtime.harness_enabled` from old config files. Install the pinned
SDK/runtime, choose a valid loopback Qwen backend and use `alfred --doctor`.
The original default-off and silent Qwen fallback documented for Issue #13
are **obsolete and must not be used**. There is no flag to disable Cordis
for interactive planning. To recover from broken installation, stop Alfred,
repair the pinned wheels/configuration, and restart; no previously staged
approval is replayed. Headless reminders/workflow administrative commands
remain independent.

Historical security audit: `docs/HARNESS_PR27_REVIEW.md` (Phase 5,
pre-mandatory adapter; its old opt-in notes are historical only).
