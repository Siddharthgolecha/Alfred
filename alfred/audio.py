"""PulseAudio/PipeWire capture with BlueZ headset profile negotiation.

Sony WI-C100 exposes its microphone using HFP/HSP, not A2DP.
Never use a monitor source as a microphone.
"""
from __future__ import annotations

from array import array
from collections import deque
import json
import logging
import os
import re
import select
import subprocess
import time

LOG = logging.getLogger(__name__)
RATE = 16000

class AudioUnavailableError(RuntimeError):
    """No acceptable live microphone is accessible through the audio server."""


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

def source_status(source, explicitly_selected=False):
    """Classify advertised sources; a listed Pulse source is not necessarily a mic.

    SoC "Built-in Audio" without a positively available input port is not
    auto-selected. Explicit direct_source still requires live capture.
    """
    name=str(source.get("name") or "")
    lower=name.lower()
    props=source.get("properties") or {}
    description=str(props.get("device.description") or props.get("device.alias") or name)
    monitor=source.get("monitor_of_sink")
    if (not name or ".monitor" in lower
            or monitor not in (None, "n/a", 4294967295, "4294967295")
            or str(props.get("device.class") or "").lower()=="monitor"):
        return False,"playback monitor or non-input source"
    if lower.startswith(("auto_null","null","remap","virtual")):
        return False,"virtual source, not a verified microphone"
    if source.get("mute") is True:
        return False,"recording source is muted"
    ports=source.get("ports") or []
    if isinstance(ports,dict):
        ports=[dict(v,name=k) for k,v in ports.items()]
    available=[str(p.get("available","unknown")).lower() for p in ports]
    if ports and all(x in ("no","false","0") for x in available):
        return False,"all input ports are unavailable/disconnected"
    active=source.get("active_port") or {}
    if isinstance(active,dict) and str(active.get("available","unknown")).lower() in ("no","false","0"):
        return False,"active microphone port is unavailable/disconnected"
    if lower.startswith("bluez_input"):
        return True,"Bluetooth microphone source; must pass capture probe"
    if explicitly_selected:
        return True,"explicit direct source; must pass capture probe"
    bus=str(props.get("device.bus") or "").lower()
    if "usb" in bus or lower.startswith("alsa_input.usb_"):
        return True,"USB input; must pass capture probe"
    onboard=("built-in" in description.lower() or "builtin" in description.lower()
             or lower.startswith("alsa_input.platform") or "onboard" in description.lower())
    if onboard:
        return False,(
            "built-in/SoC audio is not auto-trusted as a connected microphone; "
            "set audio.direct_source explicitly for a known wired/I2S capture device"
        )
    if ports and any(x in ("yes","true","1") for x in available):
        return True,"input port reports available; must pass capture probe"
    return False,"physical input not confirmed; configure direct_source explicitly"

class AudioManager:
    """Auto mode prefers WI-C100 HFP mic, then an approved direct microphone."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.active_source = None
        self.process = None
        self.last_check = 0
        self.last_reconnect_attempt = 0
        self._prefetched = b""

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
        except (OSError, ValueError, RuntimeError) as exc:
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
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                LOG.warning("Bluetooth profile %s failed: %s", p.get("name"), exc)
        return False

    def _sources(self):
        data=pactl_json("sources")
        return [s for s in data if source_status(
            s,explicitly_selected=s.get("name")==self.cfg.get("direct_source"))[0]]

    def diagnostics(self):
        """All advertised inputs with rejection reasons; no false 'mic connected' claim."""
        entries=[]
        for src in pactl_json("sources"):
            ok,reason=source_status(
                src,explicitly_selected=src.get("name")==self.cfg.get("direct_source"))
            entries.append({"name":src.get("name",""),
                            "description":(src.get("properties") or {}).get("device.description",""),
                            "eligible":ok,"reason":reason})
        return entries

    def candidate_sources(self) -> list:
        """Return eligible source names ordered by preference."""
        mode=self.cfg["mode"]
        if mode!="direct":
            if self._headset_card() is None:
                self._try_reconnect()
            self._enable_headset_mic()
        try:
            sources=self._sources()
        except (RuntimeError,OSError,ValueError) as exc:
            raise AudioUnavailableError(
                "Cannot enumerate audio inputs. Verify PipeWire/PulseAudio and "
                f"access to its session socket: {exc}") from exc
        target_card=self._headset_card() if mode!="direct" else None
        bt=[]
        if target_card:
            suffix=target_card["name"].lower().replace("bluez_card.","")
            bt=[s for s in sources if s.get("name","").lower().startswith("bluez_input")
                and (suffix in s.get("name","").lower()
                     or suffix.replace("_",":") in _descrip(s).lower())]
        direct=[s for s in sources if not s.get("name","").lower().startswith("bluez_")]
        wanted=self.cfg.get("direct_source","")
        if wanted:
            direct=[s for s in direct if s["name"]==wanted]
        else:
            direct.sort(key=lambda s: (
                not ("usb" in _descrip(s).lower() or s["name"].startswith("alsa_input.usb_")),
                s["name"]))
        if mode=="direct":
            return [s["name"] for s in direct]
        if mode=="bluetooth":
            return [s["name"] for s in bt]
        return [s["name"] for s in bt]+(
            [s["name"] for s in direct] if self.cfg.get("auto_fallback",True) else [])

    def choose_source(self) -> str | None:
        """Return the highest-priority eligible source, or None."""
        candidates=self.candidate_sources()
        return candidates[0] if candidates else None

    def _open(self,source):
        args=["parec","--device",source,"--raw","--format=s16le",
              "--rate",str(RATE),"--channels","1"]
        try:
            process=subprocess.Popen(args,stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,bufsize=0)
        except OSError as exc:
            raise AudioUnavailableError(
                f"Cannot launch parec for {source}; install pulseaudio-utils: {exc}") from exc
        self.process=process
        self.active_source=source
        self.last_check=time.monotonic()
        self._prefetched=b""
        LOG.info("Opening microphone: %s (%d Hz)",source,RATE)

    def _read_pcm(self,nbytes,timeout):
        """Bounded live stream read; never hang on false or broken capture devices."""
        if self.process is None or self.process.stdout is None:
            raise AudioUnavailableError("Audio capture is not running")
        end=time.monotonic()+float(timeout)
        buf=bytearray()
        while len(buf)<nbytes:
            if self.process.poll() is not None:
                raise AudioUnavailableError(
                    f"parec terminated for {self.active_source} "
                    f"(exit {self.process.returncode})")
            remaining=end-time.monotonic()
            if remaining<=0:
                raise AudioUnavailableError(
                    f"No audio frames received from {self.active_source} within {timeout}s")
            readable,_,_=select.select([self.process.stdout],[],[],remaining)
            if not readable:
                continue
            data=os.read(self.process.stdout.fileno(),nbytes-len(buf))
            if not data:
                raise AudioUnavailableError(
                    f"Audio stream closed unexpectedly: {self.active_source}")
            buf.extend(data)
        return bytes(buf)

    def ensure_input(self):
        """Require an eligible source AND live PCM delivery before voice startup."""
        candidates=self.candidate_sources()
        if not candidates:
            self.close()
            raise AudioUnavailableError(
                "No usable microphone detected. Connect Sony WI-C100 using HFP/HSP "
                "or attach a USB/direct mic. 'Built-in Audio' without a confirmed "
                "capture port is not accepted. Run 'alfred --doctor' for diagnostics.")
        failures=[]
        for source in candidates:
            self.close()
            try:
                self._open(source)
                self._prefetched=self._read_pcm(
                    RATE//10*2,self.cfg.get("probe_timeout_seconds",5))
                LOG.info("Validated microphone: %s (live PCM received)",source)
                return source
            except AudioUnavailableError as exc:
                failures.append(f"{source}: {exc}")
                self.close()
        raise AudioUnavailableError(
            "No usable microphone delivered PCM. "+"; ".join(failures)+
            ". Check Bluetooth HFP, the input ports, or permissions.")

    def close(self):
        p, self.process = self.process, None
        self.active_source = None
        self._prefetched = b""
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
        """Produce PCM buffers or raise AudioUnavailableError; never retry forever."""
        samples=RATE*int(self.cfg["frame_ms"])//1000
        nbytes=samples*2
        if self.process is None:
            self.ensure_input()
        consecutive_failures=0
        while True:
            if time.monotonic()-self.last_check>float(self.cfg["poll_seconds"]):
                current=self.choose_source()
                self.last_check=time.monotonic()
                if current!=self.active_source:
                    LOG.info("Microphone changed %s -> %s",self.active_source,current)
                    self.ensure_input()
            try:
                data=self._prefetched
                self._prefetched=b""
                if len(data)<nbytes:
                    data+=self._read_pcm(nbytes-len(data),
                                         self.cfg.get("read_timeout_seconds",5))
                if len(data)>nbytes:
                    self._prefetched=data[nbytes:]
                    data=data[:nbytes]
            except AudioUnavailableError as exc:
                consecutive_failures+=1
                self.close()
                if consecutive_failures>=3:
                    raise AudioUnavailableError(
                        "Microphone capture repeatedly stopped delivering audio "
                        "after 3 recovery attempts; stopping Alfred."
                    ) from exc
                # Auto can recover by selecting a different healthy direct mic;
                # if none exists this raises a fatal, actionable error.
                self.ensure_input()
                continue
            consecutive_failures=0
            pcm=array("h")
            pcm.frombytes(data)
            yield [value/32768.0 for value in pcm]

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
