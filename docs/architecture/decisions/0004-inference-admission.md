# 4. Admit native inference one request at a time

**Status:** In effect

## Context

The target device has 8 GB of memory shared between CPU and GPU. Whistle, Needle, Qwen and the
Harness together can exhaust it, and swap use on such a device makes everything slow.

## Decision

An in-process gate, `InferenceAdmission`, lets one inference request run at a time. Requests wait
at most `runtime.resource_max_wait_ms`. Before a request runs, the gate checks available RAM, swap
in use and CPU load per core. If a figure cannot be measured, the request is refused.

Voice has priority: while audio is being transcribed, Qwen and Harness requests wait or are
refused, and the background Qwen watchdog skips its check. Whistle ignores the CPU-load check but
not RAM or swap. Rejected requests are not retried by the gate. The gate can be disabled with
`runtime.resource_admission_enabled: false`.

## Consequences

- Under pressure Alfred refuses work instead of thrashing.
- The gate is a thread-level queue. It cannot interrupt a native call that has already started.
- Tools and the scheduler do not claim a slot.
- Thresholds must be tuned on real hardware. They have not been validated on a Jetson.

## Where

[`alfred/inference_admission.py`](../../../alfred/inference_admission.py). See
[Concurrency and shutdown](../concurrency.md#inference-admission).
