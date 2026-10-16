# Wake word

In voice mode Alfred acts only on speech that starts with the wake phrase. Gating happens
before Needle, Qwen or any tool sees the text. `--text`, local approvals and headless
automations do not use the wake word.

Design rationale: [ADR 0001](../architecture/decisions/0001-wake-word-gate.md).

## Say it

| You say | Result |
|---|---|
| "Alfred, check GPU status." | Runs `check GPU status`. |
| "Hey Alfred, list reminders." | Runs `list reminders`. |
| "Alfred" (pause) "check GPU status" within 8 s | Runs once. |
| "Check GPU status" | Ignored. |
| "I was talking about Alfred." | Ignored. |
| "Alfred, close" | Shuts Alfred down. See [below](#stop-by-voice). |

Rules:

- The phrase must be at the **start** of a finished utterance. Matching is case-insensitive.
- Accepted greetings before the phrase are `hey`, `hi`, `hello`, `ok` and `okay`.
- The phrase must be followed by whitespace or punctuation. "Alfredo" does not match.
- The wake prefix is removed before routing.
- Unfinished (partial) recognizer text never authorizes anything.
- A phrase on its own opens exactly **one** follow-up. The next non-empty utterance uses it up,
  and the window also closes after `wake_word.followup_seconds`. Say the phrase again for a new
  command.

## Modes

### Transcript gate (default)

`wake_word.mode: "transcript"` needs no extra dependency. Whistle transcribes **every voiced
utterance** locally, and only utterances that start with the phrase go further. The saving is
that Needle, Qwen and tools do not process unrelated speech. It works with whichever microphone
Alfred selected.

(acoustic-gating)=
### Acoustic gating

`wake_word.mode: "openwakeword"` runs a small local detector on the audio **before** Whistle, so
Whistle only runs on speech that triggered the detector. This lowers idle compute.

- The detector reads 16 kHz mono PCM in 80 ms windows (1280 samples) and keeps at least 2 s of
  pre-roll, so the start of the phrase is not cut off.
- You must supply an openWakeWord **ONNX model trained for your phrase**. Alfred does not
  download or ship a pretrained "Alfred" model.
- Startup fails with an error if `wake_word.model_path` is not a file, or if `openwakeword` or
  `numpy` is missing.
- On the Jetson, make sure the openWakeWord and ONNX Runtime builds match your Python and
  JetPack versions.

```bash
pip install -e '.[wakeword]'
```

```json
{
  "wake_word": {
    "enabled": true,
    "phrase": "Alfred",
    "mode": "openwakeword",
    "followup_seconds": 8,
    "model_path": "/home/jetson/models/alfred.onnx",
    "threshold": 0.75
  }
}
```

## Settings

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Set to `false` to restore always-listening behaviour: every transcript is processed. |
| `phrase` | `"Alfred"` | The phrase to match. |
| `mode` | `"transcript"` | `transcript` or `openwakeword`. |
| `followup_seconds` | `8` | Follow-up window after a lone wake. `0` disables follow-ups. Range 0 to 30. |
| `model_path` | `""` | ONNX model, required for `openwakeword`. |
| `threshold` | `0.75` | Detector score needed to wake. Above 0, at most 1. |

Full validation rules: [Configuration reference](../reference/configuration.md#wake_word).

## Stop by voice

After wake authorization, a command that consists only of `close`, `exit`, `quit`,
`stop listening`, `shut down` or `shutdown` (optionally followed by `.`, `!` or `?`) makes
Alfred print "Stopping Alfred" and leave the listening loop. This is handled locally and never
reaches Needle or Qwen. Background speech that merely says "close" is ignored while wake gating
is on.

## Security

A detector score or a matched phrase is **not speaker authentication**. Anyone who says the
phrase can wake Alfred, and false activations are possible. High-impact actions still require
[local approval](approvals.md), which voice can never give.
