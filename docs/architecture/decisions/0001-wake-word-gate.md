# 1. Gate voice commands behind a wake phrase

**Status:** In effect

## Context

An always-listening microphone hears background speech. Anything it transcribes could otherwise
reach Needle, Qwen and the tools.

## Decision

Voice input is acted on only when it starts with the wake phrase (default "Alfred", optionally
prefixed by "hey", "hi", "hello", "ok" or "okay"), or arrives within `wake_word.followup_seconds`
after a bare wake. A single follow-up utterance consumes the window; a lone wake word renews it.
Nothing keeps the microphone open indefinitely.

The phrase is matched literally: it is escaped, so it is never interpreted as a regular
expression, and it must be followed by whitespace or punctuation.

Two detectors exist. `transcript` (default) uses Whistle output and needs no extra dependency.
`openwakeword` gates audio *before* Whistle runs, using a user-supplied ONNX model, to cut idle
compute.

`--text` and scheduled tasks do not use the gate.

## Consequences

- Unaddressed speech never reaches a model or a tool.
- `wake_word.enabled: false` turns the gate off. Every transcript is then processed.
- No pretrained "Alfred" acoustic model is bundled, so `openwakeword` mode needs your own model.

## Where

[`alfred/wakeword.py`](../../../alfred/wakeword.py),
[`alfred/listener.py`](../../../alfred/listener.py). See the [Wake-word guide](../../guides/wake-word.md).
