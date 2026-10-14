# Alfred intrinsic_jetson — external ext4 SSD and offline meeting minutes

Target: NVIDIA Jetson Orin Nano **8 GB** with Sony WI-C100, additional independent USB and/or Bluetooth mic sources, a removable USB-A/SATA ext4 SSD containing model downloads and an existing **16 GB swapfile**, and local Qwen 3.5 4B Bartowski **Q4_K_M** served by llama.cpp. The core Alfred application on `main` is unchanged; this optional module is a Jetson-only branch.

## What "offloading to SSD swap" actually means

Linux swap is disk-backed **memory eviction**, not an executable compute target, extra GPU VRAM, or another CPU. Hot model weights, inference computation, microphones and Whistle's real-time buffers **must reside in RAM** when used. Force-swapping hot activations across USB will usually increase latency and I/O pressure drastically. Jetson's GPU and CPU share the 8 GB RAM; swap cannot give the GPU a physically resident 16 GB activation workspace.

This implementation is deliberately conservative:
- **Keep resident**: Qwen hot layers and KV cache, microphone capture buffers, Whistle/Needle active weights while they execute, OS/audio server, small working buffers.
- **Put on SSD**: llama.cpp GGUF model file *on disk* (OS memory maps pages as needed), HF hub cache, HF datasets cache, intermediate mono 16 kHz PCM WAVs, structured transcripts, note reductions, PDF, QR PNG, temporary files.
- **Serialize memory-heavy stages**: record multiple sources concurrently to SSD with small buffers; after recording, run Whistle on channels **one at a time**; then call Qwen for meeting notes/PDF. Do not host several ASR/Qwen instances concurrently.
- **Use the existing swapfile for cold memory**: Linux will decide whether to evict background pages. This code never calls `swapon`, `swapoff`, `sysctl vm.swappiness`, `mlock`, or `drop_caches`. Existing zram is not disabled.
- **Never silently fall back to onboard storage** if the external SSD fails, unmounts, changes source, is not ext4, is not writable, or lacks the reserved free-space margin. A USB disconnect stops the job with an explicit error; partially captured WAVs remain on the SSD if available.
- **Memory pressure**: a physical RAM headroom check (default 384 MiB) occurs before heavy work and during capture. Swap usage above 85% raises a warning; it is not artificially filled to improve a metric.

### Inspect the existing board before changing anything

```bash
lsblk -f
findmnt -T /YOUR/SSD/MOUNT
swapon --show --bytes
free -h
cat /proc/pressure/memory
```

Check that your SSD is really mounted separately, formatted ext4 and writable, and your 16 GB swapfile is active. Your paths were not provided; replace the examples. Do **not** run `mkswap` on a mounted ext4 data partition or recreate the existing swapfile. Do not put Qwen's model weights or a swapfile on the Jetson root filesystem as an accidental fallback.

### Install optional offline dependencies

```bash
git switch intrinsic_jetson
python -m pip install -e '.[jetson]'
sudo apt install -y bluez-tools pulseaudio-utils
```

Install these Python packages **before taking the board offline**. PDF authoring uses ReportLab; QR PNG generation uses qrcode/Pillow. The existing Cactus/Whistle and llama.cpp installation is reused. The optional BlueZ `bt-obex` program is needed only when sending to a recipient supporting Object Push.

### SSD diagnosis

```bash
alfred-jetson --ssd /YOUR/SSD/MOUNT --swapfile /YOUR/SSD/MOUNT/16GB.swap diagnose
alfred-jetson --ssd /YOUR/SSD/MOUNT --swapfile /YOUR/SSD/MOUNT/16GB.swap caches
```

The second command creates guarded SSD cache folders and prints `HF_HUB_CACHE`, `HF_DATASETS_CACHE`, `TMPDIR`. Export them in the environment of the Alfred service/download process **before importing transformers or datasets**. `HF_HOME` is intentionally left untouched so your other HF settings can stay on the onboard disk. The cache directories are created with restrictive permissions.

The swapfile argument is optional but recommended to verify your known file. Use `--require-active-swap` if an inactive swapfile should fail the preflight. `--min-free-gib` (default 2) and `--min-ram-mib` (default 384) are safety margins, **not** commands to allocate that much memory. The program never invokes privileged operations or mounts the SSD itself.

## Meeting capture: each microphone must be a distinct audio source

```bash
alfred-jetson --ssd /YOUR/SSD/MOUNT sources
pactl -f json list sources
```

Plug USB microphones into a powered USB hub if needed, connect Bluetooth microphones in HFP/HSP microphone mode, then check that each appears as its own `alsa_input.*` or `bluez_input.*` PulseAudio/PipeWire recording source.

**One mic per person** gives hardware-assisted *speaker assignment*, not perfect diarization: cross-talk, overlapping speech, microphone pickup, Bluetooth buffering, USB capture jitter and independent sample clocks still complicate attribution and alignment. All microphones on one USB hub do **not** automatically share a sample clock or identical USB polling. Some Bluetooth stacks cannot record from several handsfree headsets concurrently; verify what the actual Jetson audio server exposes.

For alignment, the recorder stores the estimated first audio sample arrival wall-clock timestamp and a sample-accurate offset within each channel. Optionally provide independently measured `offset_ms` and `drift_ppm` calibration for each mic. **Timestamps represent audio windows, not precise word start/end**, unless a future forced alignment stage is added. NTP is not needed for local relative order, but independent device clocks can drift; a shared clap at the start/end is helpful for calibration.

### Automatic microphone enumeration and speaker-channel labels

Omit `--mic` to let Alfred discover and live-probe every eligible USB microphone attached to a hub and every Bluetooth microphone already exposed by PipeWire/PulseAudio. It rejects playback monitors, fake Built-in Audio sources and microphones that fail a bounded PCM probe. It sorts input device names deterministically and assigns `USB_1`, `USB_2`, ... and `Bluetooth_1`, etc. It caps automatic selection at **eight** independent sources (`--max-mics 1..8`). `alfred-jetson ... sources` prints the currently usable source count.

These labels are **recording channels, not proven speaker identities**. One participant can be picked up by multiple microphones. Manual `--mic Alice=...` remains supported when assigning a microphone to a known participant.

The USB hub is not a shared ADC clock. When feasible, deliberately clap once near the start (and once near the end) and add `--clap-sync` or `--clap-sync --two-claps`. Alfred estimates per-channel offset and, with two reliable claps, sample-clock drift. It saves the calibration and uncertainty in `calibration.json` and `transcript.json`; if calibration is unavailable, it explicitly records that fact instead of fabricating sub-sample synchronization. In heavy Bluetooth buffering or fluctuating latency, calibration may still be imperfect.

Whistle transcribes bounded 12-second audio pieces sequentially, retaining **word-level start/end timestamps and confidence** from its native API when available, and marks window-level timing when only the legacy streaming API is supported. Distinct overlapping speakers retain separate timeline events. Strong identical cross-talk transcripts can be marked as duplicates, but all raw recognized events remain archived in `transcript.json`. The Qwen minutes generator receives the cleaned speaker-labeled, overlap-annotated transcript. Timestamps are decoder estimates (roughly 80 ms frame scale), not exact ADC sample/word boundaries.

The meeting CLI shows what every input captured. During post-recording transcription, interactive terminals update fixed rows for each channel instead of printing repeated partial phrases; non-interactive terminals emit timestamped confirmed speaker events. For best-effort **while-recording** transcription, add `--live-preview`. This starts a single dedicated Whistle worker behind a bounded queue; if it cannot keep up, previews are skipped instead of losing the primary SSD audio. After recording, Alfred still re-transcribes the complete channels for the final minutes. Live previews use additional CPU/RAM and can lag behind actual speech.

### Record → ASR → Qwen → PDF → QR

```bash
alfred-jetson --ssd /YOUR/SSD/MOUNT \
  --swapfile /YOUR/SSD/MOUNT/16GB.swap \
  meeting --consent --seconds 600 --title "Project Review" \
  --live-preview --clap-sync --max-mics 8

# Optional explicit speaker/channel mapping:
alfred-jetson --ssd /YOUR/SSD/MOUNT \
  meeting --consent --seconds 600 --title "Project Review" \
  --mic Alice=alsa_input.usb-mic-one.analog-mono \
  --mic Bob=alsa_input.usb-mic-two.analog-mono \
  --mic Charlie=bluez_input.AA_BB_CC_DD_EE_FF.0
```

To supply measured calibration, append optional signed integer milliseconds and parts-per-million:

```bash
--mic Alice=alsa_input.usb-mic-one.analog-mono,42,30
```

**Only record after notifying and obtaining appropriate consent from participants.** `--consent` is a required local acknowledgement. Choose the actual source names from `sources`; example names are placeholders.

The workflow writes under `<SSD>/alfred/meetings/<UTC-session-id>/`:

- `USB_1.wav`, `USB_2.wav`, `Bluetooth_1.wav`, etc. in auto mode, or your explicit `Alice.wav`/`Bob.wav` speaker labels — separate 16 kHz PCM recordings.
- `capture_manifest.json` — capture clock estimates and WAV paths; optional `calibration.json` records clap-estimated offset/drift and uncertainty.
- `transcript.json` — Whistle word timestamps/confidence, raw channel events, overlap and possible cross-talk markers, calibration; `transcript.txt` — ordered speaker-channel events supplied to Qwen.
- `minutes.json`, `minutes.pdf` — validated Qwen synthesis and PDF output.
- `handoff.json`, `handoff-qr.png` — file checksum, device metadata and offline transfer instructions (no transcript content in QR).

If Qwen or PDF generation fails after recording, resume on the same files:

```bash
alfred-jetson --ssd /YOUR/SSD/MOUNT \
  resume --session /YOUR/SSD/MOUNT/alfred/meetings/SESSION_NAME \
  --title "Project Review"
```

The existing `llama-server` at `qwen.url` is reused; if absent and `qwen.model_path`/`qwen.binary` are set, Alfred can start it. Keep the GGUF on the SSD. The minutes synthesis happens **after capture**, in bounded transcript chunks followed by a reduction pass. The reasoner returns one structured `save_meeting_pdf` tool call, whose arguments are validated and rendered with ReportLab; it cannot choose arbitrary output paths or invoke shell tools.

## Bluetooth QR handoff: compatibility constraints

A camera scanning a generic QR **cannot automatically retrieve a PDF over Bluetooth**. Scanning does not supply a standardized cross-platform Bluetooth file-transfer session, receiver consent, or an app-side file protocol. The handoff QR encodes adapter MAC (if present), PDF filename, size, SHA-256, transport type and instructions. **It is metadata only, not an active download link.** It is generated immediately after the PDF.

For a device already paired/trusted with Jetson and running an OBEX Object Push receiver, explicitly request a transfer by passing:

```bash
alfred-jetson --ssd /YOUR/SSD/MOUNT resume \
  --session /YOUR/SSD/MOUNT/alfred/meetings/SESSION_NAME \
  --recipient 11:22:33:44:55:66
```

To resend an **existing** PDF without running Qwen or Whistle again, use:

```bash
alfred-jetson --ssd /YOUR/SSD/MOUNT \
  send --session /YOUR/SSD/MOUNT/alfred/meetings/SESSION_NAME \
  --recipient 11:22:33:44:55:66
```

The command verifies the PDF against the original QR's SHA-256 before sending.
It does not retranscribe or reload the reasoning model.

`--recipient` transfers using BlueZ `bt-obex -p` **only after** checking the recipient is already paired and trusted. The receiving device must explicitly accept the push. Android receivers can support OBEX OPP; many iOS configurations do not expose this profile. A recipient scanning a QR does **not** by itself trigger the transfer.

An automatic scan-to-download Bluetooth experience for arbitrary phones requires building/installing a companion mobile app and custom BLE/GATT or RFCOMM transfer protocol, or using an offline local Wi-Fi hotspot with HTTPS and QR. Neither is silently substituted here. The workflow never starts internet/cloud sharing.

## Safety / acceptance

- No root actions; inspect SSD and active swap read-only; verify stable ext4 mount source before each guarded stage.
- Data stays in private directories (process umask 077), with no automatic onboard fallback.
- Meeting length capped at 4 hours and source count at 8.
- Capture failures abort generation, rather than fabricate silent or missing speaker transcripts.
- Memory-heavy ASR channels and meeting Qwen are sequential; local inference occurs via loopback server.
- No word-exact diarization claim; audio window timestamps and calibration metadata are explicit.
- QR checksum helps verify a transferred PDF; receipt on receiver must be checked.
- Unit tests use simulated mounts and microphones. **Real 8 GB Jetson SSD unplug/pressure tests, multiple simultaneous HFP streams, exact Whistle event format, llama.cpp schema output, PDF fonts and Bluetooth OPP interoperability remain unverified on hardware.**

Related primary references: [Jetson AI Lab RAM optimization](https://www.jetson-ai-lab.com/tutorials/ram-optimization/), [BlueZ Object Push](https://github.com/bluez/bluez/blob/master/doc/org.bluez.obex.ObjectPush.rst).
