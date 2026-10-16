# 2. Require the DeepSeek Harness and keep it tool-free

**Status:** In effect

## Context

Qwen needs a hosting layer for planning. A hosting layer with its own tools, network or shell
access would give a language model authority that Alfred's allowlists do not control.

## Decision

Alfred uses the official DeepSeek Harness SDK, pinned to an exact version, as the only Qwen
host. Interactive modes (voice and `--text`) refuse to start unless the Harness audit passes and an
SDK child process launches. The audit compares the effective Cordis plugin graph with Alfred's
overlay and fails closed on any difference.

The Harness is **tool-free**: its plugin rows are denied or disabled. Alfred keeps all tool
authority. The Harness returns a plan as data, and Alfred validates and, if needed, stages it for
approval.

## Consequences

- Qwen cannot execute anything directly, even if its output is adversarial.
- A missing or mismatched Harness stops Alfred from starting. There is no silent fallback.
- Upgrading the Harness means re-auditing the plugin graph and updating the pin and checksums.
- `qwen.enabled: false` is incompatible with voice and `--text` modes.
- The tool bridge between the Harness and Alfred's tools is not wired (`--doctor` prints
  `Cordis tool bridge: NOT YET WIRED`).

## Where

[`alfred/cognitive/harness_sdk.py`](../../../alfred/cognitive/harness_sdk.py),
[`harness_audit.py`](../../../alfred/cognitive/harness_audit.py),
[`harness_config.py`](../../../alfred/cognitive/harness_config.py),
[`alfred_safe.patch.yml`](../../../alfred/cognitive/alfred_safe.patch.yml). See
[DeepSeek Harness](../harness.md).
