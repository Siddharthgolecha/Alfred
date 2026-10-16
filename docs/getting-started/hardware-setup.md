# Hardware setup

Microphone selection, Bluetooth pairing and the on-device acceptance checklist.

:::{important}
None of this has been validated on a Jetson. Bluetooth headset profile support on your JetPack
image, the native Cactus ARM64 binaries, speed, memory and spoken feedback are all unverified.
:::

## How Alfred picks a microphone

Alfred records with `parec` from a PulseAudio **source** (16 kHz, mono, signed 16-bit). It never
records from a sink or a `.monitor` source. Set the strategy with `audio.mode`:

| `audio.mode` | Behaviour |
|---|---|
| `auto` (default) | Prefers a Bluetooth headset (`bluez_input*`). If none works and `audio.auto_fallback` is true, uses a direct source. |
| `bluetooth` | Bluetooth headset only. No silent fallback. |
| `direct` | `audio.direct_source` if set, otherwise a detected ALSA or USB microphone. |

With `audio.auto_fallback: false`, `auto` mode also refuses to fall back to a direct source.

A source is accepted only if the audio server advertises it **and** live PCM actually arrives
within `audio.probe_timeout_seconds` (default 5). A label is never treated as proof.

### The "Built-in Audio" trap

On some Orin Nano images the audio server lists **Built-in Audio** although no microphone is
attached, because the name describes an onboard controller. Alfred rejects such generic
onboard inputs from automatic selection. If a real microphone is wired to onboard audio, set
`audio.direct_source` to its name from `pactl list sources`. The live PCM probe still applies.
Ports known to be unavailable are never accepted.

### Failure behaviour

| Situation | Result |
|---|---|
| No usable microphone at start | Voice mode exits with status **2**. Cactus models and the backend watchdog are not started. |
| Audio drops while listening | Alfred tries another candidate. After three consecutive recovery failures it raises an audio error and exits. |
| Bluetooth headset disconnects (address configured) | Alfred runs `bluetoothctl connect <address>`, at most once every 30 s. |

`alfred.service` sets `RestartPreventExitStatus=2`, so a missing microphone stops the service
instead of restart-looping. After connecting a microphone, run `systemctl --user restart alfred`.
`--text`, `--reason`, `--ensure-backend`, `--scheduler-only` and the approval commands need no
microphone.

Silence on a working stream is not a failure. PCM delivery is a functional check, not a
guarantee that a microphone is physically present on every driver.

## Pair the Sony WI-C100

1. Put the headset in pairing mode. It is a Bluetooth neckband. Its USB-C port is for charging
   and is **not** a wired microphone.
2. Pair, trust and connect with BlueZ:

   ```bash
   bluetoothctl
   power on
   agent on
   default-agent
   scan on
   # once the headset's address is listed:
   pair XX:XX:XX:XX:XX:XX
   trust XX:XX:XX:XX:XX:XX
   connect XX:XX:XX:XX:XX:XX
   quit
   ```

3. Put the address in `audio.bluetooth_address` in your untracked `config.json`. This pins that
   headset and enables reconnection attempts.
4. Check that the headset exposes a microphone:

   ```bash
   pactl -f json list cards
   pactl -f json list sources
   ```

   The headset must offer a **headset or handsfree** profile (HFP or HSP). A2DP alone gives
   playback but no microphone. If A2DP works but no `bluez_input.*` source appears, fix HFP/HSP
   support in your Bluetooth and audio-session configuration first. In `auto` mode Alfred
   switches the card to the headset profile with `pactl set-card-profile`.
5. Run `alfred --doctor`, then start Alfred.

Switching to HFP/HSP lowers playback quality compared with A2DP. This is normal.

## Tune speech detection

Utterances are detected by an RMS energy threshold. If background noise triggers capture, or
quiet speech is missed, adjust `audio.speech_rms` (default `0.013`, allowed 0.001 to 0.2) using
recordings of your own speech and noise. `audio.end_silence_frames` and
`audio.max_utterance_seconds` control where an utterance ends.

## Diagnostics

```bash
pactl -f json list sources
pactl -f json list cards
alfred --doctor
echo $?    # 0: working microphone, 2: no valid input
```

## Hardware acceptance checklist

Run this on the board before trusting a deployment. CI cannot establish Bluetooth or CUDA
behaviour.

- [ ] The headset pairs and its microphone appears in `pactl` with an HFP/HSP profile.
- [ ] `--doctor` lists the Bluetooth and direct sources.
- [ ] Direct mode works with a USB or analog microphone.
- [ ] `auto` mode switches from the headset to direct on disconnect, and back on reconnect.
- [ ] `--ensure-backend` adopts or starts the backend and verifies the model.
- [ ] `/v1/models` shows the intended Qwen model. Use `tegrastats` to monitor the GPU, because
      `nvidia-smi` is generally not the Jetson method.
- [ ] Cactus loads Needle and Whistle, and streaming transcription produces words.
- [ ] Voice actions route correctly and nothing executes on unconfirmed text.
- [ ] Reminders and schedules survive a service restart.
- [ ] Continuous capture stays responsive during Qwen inference. Record P50 and P95 latency and
      RAM use.

For the formal Jetson release criteria see [Release process](../development/release-process.md).
