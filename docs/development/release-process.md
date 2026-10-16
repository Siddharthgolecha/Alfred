# Release process

Alfred has not been released. There are no tags, and `pyproject.toml` declares version `0.1.0`.
This page describes the **gate** that a Jetson release must pass, as implemented in
[`alfred/release_gates.py`](../../alfred/release_gates.py), and the steps around it.

:::{important}
Green CI does not qualify a release. The release gate is a checklist validator, not a device
attestation system. It cannot verify the claims in a report, so a hardware owner must also sign
off and review the raw device runs.
:::

## The gate

`evaluate_release(report, thresholds, *, acknowledge_hardware=False)` returns a verdict
`{release_allowed, reasons, checked_metrics, policy}`. `require_release` raises `ReleaseBlocked`
with the reasons joined by `; ` if the release is not allowed. Release is allowed only when
**every** check below passes. There are no defaults or placeholder values.

| Requirement | Report field | Rule |
|---|---|---|
| Real device | `native_jetson_tested`, `device` | `true`, and the device string is exactly `NVIDIA Jetson Orin Nano 8GB`. |
| Coverage | `fixture_count`, `distinct_semantic_task_count` | Both integers of at least 100. The distinct count cannot exceed the fixture count. Booleans, floats and strings are rejected. |
| Provenance | `sources` | A non-empty list. |
| Safety | `release_gates.unauthorized_tool_executions`, `unintended_replay` | Integers equal to 0. |
| Plugin escape | `release_gates.plugin_escape_attempts_succeeded` | Equal to 0. |
| Metrics | the eight below | Each measured, finite and non-negative, with an agreed `max_<metric>` threshold greater than 0 that it does not exceed. |
| Models | `models_real`, `offline` | Both `true`. |
| Deadlines | `voice_capture_deadline_passed`, `scheduler_deadline_passed` | Both `true`. |
| Shutdown | `clean_owned_shutdown` | `true`. |
| Dependencies | `dependencies_merged` | `true`. |
| Owner | `acknowledge_hardware` argument | `True`. |

Required metrics:

- `p50_latency_ms` and `p95_latency_ms`
- `qwen_cold_load_ms` and `qwen_warm_load_ms`
- `peak_ram_mib` and `peak_swap_used_mib`
- `peak_cpu_load_per_core`
- `peak_gpu_utilization_pct`

An offline matrix report cannot satisfy this. It records the distinct task count as `null`, and
Jetson GPU load, power and Qwen load times as `null`. They are never interpolated.

## Collect device evidence

Run these on the physical board, not in CI.

1. **Record the environment.** JetPack and L4T versions, Python and toolchain, hashes of the
   GGUF, Whistle and Needle models, RAM, power mode, backend configuration, git commit, ambient
   temperature and exact clock time.
2. **Repeat across days.** Do at least three independent cold starts per day, plus warm sessions,
   for each of: the baseline Needle-to-Qwen path, Needle loop with scoped context, and the unified
   Harness with context. Interleave the order to limit temperature and power bias.
3. **Use at least 100 distinct tasks.** The 12 mock families are not enough. Vary owner, source,
   preconditions and adversarial inputs. Label repeated trials honestly. Run consequential tasks
   against sandboxed allowed services, never production actuators.
4. **Measure.** Wall-clock P50 and P95 latency and the true outcome of each task. Cold and warm
   Qwen start from the supervisor's health probes. CPU and GPU use through `tegrastats` or a
   sysfs read. Peak RAM and swap while Whistle is streaming. Microphone overruns and drops.
   Reminder deadline lateness. Subprocess and server counts.
5. **Vary the audio.** Try one or more USB microphones and Bluetooth. Check for leakage of
   historical transcripts and for invented owners, dates or unverified transfer success in
   meeting output.
6. **Inject failures.** Crash the server while read-only and approved service tasks are in
   flight, and check that one-time side effects are **never** retried. Audit the SQLite
   approvals and child PIDs. Simulate low RAM, swap pressure, high CPU, and Ctrl+C or
   `alfred close` while Whistle is recording.
7. **Archive the raw data.** Keep native CSV and JSON and model traces with private audio and
   transcripts removed. Review thresholds with the hardware owner.

## Evaluate

After an independent review, build the report and thresholds as JSON objects and call:

```python
from alfred.release_gates import evaluate_release
verdict = evaluate_release(native_report, approved_thresholds, acknowledge_hardware=True)
```

Do not default-enable Needle feedback, persistent context, Harness features, meeting access or
inference admission because CI is green.

## Resource admission

`runtime.resource_admission_enabled` is `true` by default. It is cooperative scheduling, not
kernel GPU isolation, and it cannot preempt a native call that is already running. See
[Concurrency](../architecture/concurrency.md#inference-admission) and
[ADR 0004](../architecture/decisions/0004-inference-admission.md). Hardware tests must check
microphone buffers, capture latency, dropped frames, GPU memory fragmentation and missed
scheduler deadlines.

## Versioning and the changelog

1. Update `[Unreleased]` in [`CHANGELOG.md`](https://github.com/Siddharthgolecha/Alfred/blob/main/CHANGELOG.md) with every user-visible change as
   it merges.
2. When a release is approved, rename `[Unreleased]` to the version and date, add a fresh
   `[Unreleased]` section, and set `version` in `pyproject.toml`.
3. Tag the commit.

The `0.1.0` changelog section is reconstructed from history and is not yet a real release.
