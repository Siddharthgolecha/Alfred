# Wake-word activation

Alfred no longer treats every speech segment as a command. Wake gating is
**enabled by default** in voice mode and is applied before Needle, Qwen or
tool execution. CLI `--text`, local approvals and headless automations are
unchanged.

## Default: zero-extra-dependency transcript gate

`wake_word.mode: "transcript"` uses the existing offline Whistle recognizer.
Speech is transcribed locally, but only utterances beginning with the configured
wake phrase are eligible for routing. Nothing else is forwarded to Needle/Qwen,
executed, logged as a command or printed as a finalized transcript.

Say, for example:

- **"Alfred, check GPU status."** → runs `check GPU status`
- **"Hey Alfred, list reminders."** → runs `list reminders`
- **"Alfred"**, then within eight seconds **"check GPU status"** → runs once
- **"Check GPU status"** without activation → ignored
- **"I was talking about Alfred."** → ignored

A standalone wake opens exactly **one** bounded follow-up utterance; the next
nonempty utterance consumes the window and subsequent speech again requires
activation. The window expires after `followup_seconds`. Say the wake phrase
again to start a fresh command. Wake matching is case-insensitive, restricted
to the **start** of a finalized utterance, and never authorizes partial
recognizer hypotheses. The wake prefix is removed before routing.

The default transcription gate avoids an extra model and works with whatever
live mic AudioManager selects, including USB or a Bluetooth headset. **Whistle
still processes every voiced utterance** locally in this mode; the saving is
that Needle, Qwen and tools do not process unrelated speech.

## Optional acoustic gating (lower idle compute)

Set `wake_word.mode` to `openwakeword` to run a dedicated **local** acoustic
spotter before Whistle. It consumes 16 kHz mono PCM in 80 ms increments, adapts
to 250/500/1000 ms capture frames, and retains up to 2 seconds of pre-roll.
Only speech segments where the detector crosses its threshold are sent to
Whistle. Saying the wake phrase alone still opens a bounded follow-up command
window.

A **matching, user-provided openWakeWord ONNX wake model** is required, trained
for the phrase you choose (e.g. `Alfred`). Alfred does not fetch or claim to
ship a pretrained "Alfred" model. Startup fails with an actionable error if
the model or optional dependency is absent. On Jetson, ensure the openWakeWord
and ONNX Runtime builds are compatible with your Python/JetPack environment.

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

The ONNX model must have been trained for that specific wake expression.
A detector score above `threshold` is not speaker authentication; anyone
speaking the phrase can wake the assistant, and false activations are
possible. High-impact actions still need Alfred's separate local approval.

## Settings

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Set false only to deliberately restore legacy always-listening commands. |
| `phrase` | `"Alfred"` | Literal recognized wake prefix, also accepts "Hey Alfred" and similar greetings. |
| `mode` | `"transcript"` | `transcript` or `openwakeword`. |
| `followup_seconds` | `8` | Standalone-wake window, 0–30 seconds (0 disables follow-ups). |
| `model_path` | `""` | Required ONNX wake model path only for `openwakeword`. |
| `threshold` | `0.75` | Acoustic detection threshold (0, 1]; tune on the actual mic/noise environment. |

`--text`, background timers and scheduler workflows never require the wake
phrase. Whistle partial text is never routed to tools and unaddressed audio
is not saved as raw speech. In transcript mode, transcription happens in
memory before routing is rejected. In acoustic mode, the wake detector runs
locally on unaddressed audio instead.

## Verification

```bash
python -m unittest discover -s tests -v
python -m alfred --doctor
python -m alfred
```

Try ambient speech and confirm no commands trigger, then test both same-utterance
and standalone wake flows. Acoustic accuracy and latency must be measured on
the target Jetson hardware with the particular wake model/microphones.
