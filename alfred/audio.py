"""PulseAudio/PipeWire capture with BlueZ headset profile negotiation.

Sony WI-C100 exposes its microphone using HFP/HSP, not A2DP.
Never use a monitor source as a microphone.
"""
from __future__ import annotations

from array import array
from collections import deque
import json
import logging
import re
import subprocess
import time

LOG = logging.getLogger(__name__)
RATE = 16000

def command(*args, timeout=6):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()[:240]}")
    return result.stdout

def pactl_json(what):
    return json.loads(command("pactl", "-f", "json", "list", what))

def _descrip(item):
    p = item.get("properties") or {}
    return " ".join(str(p.get(k, "")) for k in (
        "device.description", "device.alias", "bluez5.address", "api.bluez5.address", "device.string"
    )) + " " + str(item.get("name", ""))

def _is_available(profile):
    a = str(profile.get("available", "")).lower()
    return a not in ("no", "false", "0")

def _profiles(card):
    raw = card.get("profiles") or []
    if isinstance(raw, dict):
        return [dict(v, name=k) for k,v in raw.items()]
    return raw

class AudioManager:
    """Auto mode prefers WI-C100 HFP mic, then an approved direct microphone."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.active_source = None
        self.process = None
        self.last_check = 0
        self.last_reconnect_attempt = 0

    def _try_reconnect(self):
        address = self.cfg.get("bluetooth_address", "").strip()
        if not address or time.monotonic() - self.last_reconnect_attempt < 30:
            return
        self.last_reconnect_attempt = time.monotonic()
        if not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", address):
            LOG.warning("Invalid Bluetooth address; expected MAC")
            return
        try:
            subprocess.run(["bluetoothctl", "connect", address], capture_output=True,
                           timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("Bluetooth reconnect attempt failed: %s", exc)

    def _headset_card(self):
        try:
            cards = pactl_json("cards")
        except Exception as exc:
            LOG.warning("Cannot query audio cards: %s", exc)
            return None
        address = self.cfg.get("bluetooth_address", "").lower().replace(":", "_")
        for card in cards:
            name = str(card.get("name", "")).lower()
            if not name.startswith("bluez_card"):
                continue
            desc = _descrip(card).lower()
            if address:
                if address not in name and address.replace("_", ":") not in desc:
                    continue
            elif self.cfg.get("sony_name", "WI-C100").lower() not in desc:
                continue
            return card
        return None

    def _enable_headset_mic(self):
        card = self._headset_card()
        if card is None:
            return False
        profiles = _profiles(card)
        active = card.get("active_profile", {})
        active_name = active.get("name", "") if isinstance(active, dict) else str(active)
        if any(k in active_name.lower() for k in ("headset", "handsfree", "hfp", "hsp")):
            return True
        choices = sorted(
            (p for p in profiles if _is_available(p) and any(x in str(p.get("name","")).lower()
                for x in ("headset-head-unit", "handsfree_head_unit", "headset_head_unit", "hfp", "hsp"))),
            key=lambda p: (not ("msbc" in str(p).lower() or "wideband" in str(p).lower()),
                           not ("handsfree" in str(p.get("name","")).lower() or "hfp" in str(p.get("name","")).lower()))
        )
        for p in choices:
            try:
                command("pactl", "set-card-profile", card["name"], p["name"])
                LOG.info("Switched %s to %s", card["name"], p["name"])
                time.sleep(0.5)
                return True
            except Exception as exc:
                LOG.warning("Bluetooth profile %s failed: %s", p.get("name"), exc)
        return False

    def _sources(self):
        data = pactl_json("sources")
        return [s for s in data if ".monitor" not in s.get("name","").lower()
                and s.get("monitor_of_sink") in (None, "n/a", 4294967295)]

    def choose_source(self):
        mode = self.cfg["mode"]
        if mode != "direct":
            if self._headset_card() is None:
                self._try_reconnect()
            self._enable_headset_mic()
        try:
            sources = self._sources()
        except Exception as exc:
            LOG.warning("Cannot query microphones: %s", exc)
            return None
        bt = [s for s in sources if s.get("name", "").lower().startswith("bluez_input")]
        target_card = self._headset_card() if mode != "direct" else None
        if target_card:
            suffix = target_card["name"].lower().replace("bluez_card.", "")
            bt = [s for s in bt if suffix in s.get("name", "").lower()
                  or suffix.replace("_", ":") in _descrip(s).lower()]
        else:
            bt = []
        direct = [s for s in sources if not s.get("name", "").lower().startswith("bluez_")]
        wanted = self.cfg.get("direct_source", "")
        if wanted:
            direct = [s for s in direct if s["name"] == wanted]
        else:
            direct.sort(key=lambda s: (not any(x in _descrip(s).lower() for x in ("usb", "analog", "alsa")), s["name"]))
        selected = (bt[:1] if bt else (direct[:1] if mode == "auto" and self.cfg.get("auto_fallback", True) else []))
        if mode == "direct":
            selected = direct[:1]
        if mode == "bluetooth":
            selected = bt[:1]
        return selected[0]["name"] if selected else None

    def _open(self, source):
        # parec negotiates/obtains audio from PulseAudio or PipeWire's pulse server.
        args = ["parec", "--device", source, "--raw", "--format=s16le",
                "--rate", str(RATE), "--channels", "1"]
        self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        bufsize=0)
        self.active_source = source
        self.last_check = time.monotonic()
        LOG.info("Capturing %s at %d Hz", source, RATE)

    def close(self):
        p, self.process = self.process, None
        self.active_source = None
        if p:
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)
            if p.stdout:
                p.stdout.close()

    def frames(self):
        """Yield 16 kHz mono float buffers; reconnect on failure/input changes."""
        samples = RATE * int(self.cfg["frame_ms"]) // 1000
        while True:
            if self.process is None:
                source = self.choose_source()
                if not source:
                    LOG.warning("No approved microphone found; retrying")
                    time.sleep(float(self.cfg["poll_seconds"]))
                    continue
                self._open(source)
            if time.monotonic() - self.last_check > float(self.cfg["poll_seconds"]):
                current = self.choose_source()
                self.last_check = time.monotonic()
                if current != self.active_source:
                    LOG.info("Microphone changed %s -> %s", self.active_source, current)
                    self.close()
                    continue
            data = self.process.stdout.read(samples * 2)
            if len(data) != samples * 2 or self.process.poll() is not None:
                LOG.warning("Audio stream ended; restarting capture")
                self.close()
                time.sleep(0.5)
                continue
            pcm = array("h")
            pcm.frombytes(data)
            yield [s / 32768.0 for s in pcm]

def rms(samples):
    return (sum(v*v for v in samples) / max(1, len(samples))) ** 0.5

class UtteranceStream:
    """Frame-level energy-based VAD and bounded incremental Whisper/Whistle stream."""
    def __init__(self, frames, cfg):
        self.frames = iter(frames)
        self.cfg = cfg

    def utterances(self):
        threshold = float(self.cfg["speech_rms"])
        silent_end = int(self.cfg["end_silence_frames"])
        frame_secs = int(self.cfg["frame_ms"]) / 1000
        max_frames = max(1, int(float(self.cfg["max_utterance_seconds"]) / frame_secs))
        history = deque(maxlen=2)
        while True:
            samples = next(self.frames)
            if rms(samples) < threshold:
                history.append(samples)
                continue
            pre = list(history)
            history.clear()
            def segment(first=samples, prefix=pre):
                nonlocal history
                for chunk in prefix:
                    yield chunk
                yield first
                quiet = 0
                for _ in range(max_frames - 1):
                    chunk = next(self.frames)
                    yield chunk
                    if rms(chunk) < threshold:
                        quiet += 1
                        if quiet >= silent_end:
                            break
                    else:
                        quiet = 0
                history.clear()
            yield segment()
