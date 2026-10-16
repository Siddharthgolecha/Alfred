# 5. Validate at several layers, not only in the config model

**Status:** In effect, with a known gap

## Context

Alfred has two ways to describe configuration: a plain `dict` with a hand-written `_validate()`,
and Pydantic models (`AudioConfig`, `WakeWordConfig`, `QwenConfig`, `RuntimeConfig`). Components
also check the values they consume.

## Decision

- Cognitive event types are frozen Pydantic models with `extra="forbid"` and a strict JSON codec.
- Configuration keeps the `dict` as the runtime form. `load_config()` merges the file, applies
  environment overrides and runs `_validate()`, which checks the backend, the Qwen URL and the
  wake-word settings. Unknown keys are rejected.
- The Pydantic config models add range checks. They are built by `load_config_model()`, which is
  the preferred entry point for new code.
- Components such as the context manager, the Needle loop and the Harness re-validate the values
  they receive.
- Magic numbers, limits and regular expressions live in
  [`alfred/constants.py`](../../../alfred/constants.py).

## Consequences

- A bad value is usually caught somewhere, but not always at startup.
- **The CLI calls `load_config()`, not `load_config_model()`.** Ranges checked only by the Pydantic
  models, such as `audio.frame_ms`, `audio.mode` and `runtime.approval_ttl_seconds`, are not
  enforced when Alfred starts. See [Configuration](../../reference/configuration.md#what-is-validated-and-when).
- Closing that gap means calling `load_config_model()` from the CLI. That would change startup
  behaviour, so it is a code change and not a documentation fix.

## Where

[`alfred/config.py`](../../../alfred/config.py),
[`alfred/cognitive/contracts.py`](../../../alfred/cognitive/contracts.py),
[`alfred/validators.py`](../../../alfred/validators.py).
