"""Parallel multi-source recording with one isolated capture stream per microphone.

Samples from separate USB/Bluetooth clocks are NOT hardware-synchronized.
Timestamps are calibrated capture-time estimates with explicit uncertainty.
"""
from __future__ import annotations
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
from pathlib import Path
import json
import os
import re
import select
import subprocess
import threading
import time
import wave

from ..audio import pactl_json,source_status
from .storage import GuardedSSD,StorageSafetyError

RATE=16000
CHANNELS=1
BYTES_PER_SAMPLE=2
FRAME_BYTES=RATE*BYTES_PER_SAMPLE//5  # 200 ms


@dataclass(frozen=True)
class Source:
    speaker: str
    pulse_source: str
    offset_ms: int = 0     # measured one-way audio latency correction
    drift_ppm: int = 0     # independently calibrated clock drift

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}",self.speaker):
            raise ValueError("Speaker names must be simple non-empty identifiers")
        if not self.pulse_source or len(self.pulse_source)>250:
            raise ValueError("Explicit Pulse/PipeWire source is required")
        if abs(self.offset_ms)>10000 or abs(self.drift_ppm)>2000:
            raise ValueError("Unreasonable clock calibration; check source offset/drift")


def validate_sources(sources):
    if not 1<=len(sources)<=8:
        raise ValueError("Meeting needs 1..8 independent microphone sources")
    if len({s.speaker for s in sources})!=len(sources):
        raise ValueError("Duplicate speaker labels")
    if len({s.pulse_source for s in sources})!=len(sources):
        raise ValueError("Duplicate physical microphone source")
    available={x.get("name"):x for x in pactl_json("sources")}
    for source in sources:
        item=available.get(source.pulse_source)
        if item is None:
            raise ValueError(f"Missing microphone: {source.speaker}/{source.pulse_source}")
        ok,why=source_status(item,explicitly_selected=True)
        if not ok:
            raise ValueError(f"Microphone {source.speaker} unavailable: {why}")
    return True


def iso_utc(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec="milliseconds")


def adjusted_seconds(first_unix,frame_index,s:Source,rate=RATE):
    elapsed=frame_index/rate
    return first_unix + s.offset_ms/1000.0 + elapsed*(1+s.drift_ppm/1_000_000.0)


class Recorder:
    """Small buffers, no memory-held recordings, fail closed on lost SSD/microphone."""
    def __init__(self,ssd:GuardedSSD,sources,duration_seconds,
                 on_preview=None,preview_seconds=3):
        self.ssd=ssd
        self.sources=list(sources)
        self.seconds=int(duration_seconds)
        if not 1<=self.seconds<=4*3600:
            raise ValueError("Meeting recording duration must be 1s..4h")
        self.stop=threading.Event()
        self.start=threading.Event()
        self.errors={}
        self.records={}
        self._threads=[]
        self.on_preview=on_preview
        self.preview_samples=int(preview_seconds*RATE)
        if not 2*RATE<=self.preview_samples<=10*RATE:
            raise ValueError("Preview chunk must be 2..10 seconds")

    def _record_one(self,source,folder):
        name=source.speaker
        path=folder/(name+".wav")
        proc=None
        try:
            # Audio is emitted from PulseAudio's / PipeWire's audio server
            # through an EXPLICIT source; never a monitor/default source.
            proc=subprocess.Popen(
                ["parec","--device",source.pulse_source,"--raw",
                 "--format=s16le","--rate",str(RATE),"--channels","1"],
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
            self.start.wait(10)
            if not self.start.is_set():
                raise RuntimeError("Meeting start barrier timed out")
            total=0
            first=None
            preview=bytearray()
            preview_first=0
            with wave.open(str(path),"wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(BYTES_PER_SAMPLE)
                wav.setframerate(RATE)
                end=time.monotonic()+self.seconds
                last_guard=time.monotonic()
                while time.monotonic()<end and not self.stop.is_set():
                    if proc.poll() is not None:
                        raise RuntimeError(f"parec exited for {name} with {proc.returncode}")
                    readable,_,_=select.select([proc.stdout],[],[],1.5)
                    if not readable:
                        raise RuntimeError("Microphone stream stalled")
                    block=os.read(proc.stdout.fileno(),FRAME_BYTES)
                    if not block:
                        raise RuntimeError("Microphone stream disconnected")
                    block=block[:len(block)//2*2]
                    if not block:
                        continue
                    receipt=time.time()
                    if first is None:
                        first=receipt-len(block)/(RATE*BYTES_PER_SAMPLE)
                    wav.writeframesraw(block)
                    total+=len(block)//BYTES_PER_SAMPLE
                    if self.on_preview is not None:
                        preview.extend(block)
                        window_bytes=self.preview_samples*BYTES_PER_SAMPLE
                        while len(preview)>=window_bytes:
                            chunk=bytes(preview[:window_bytes])
                            del preview[:window_bytes]
                            # A bounded non-blocking consumer must never stall
                            # microphone recording or exhaust RAM.
                            self.on_preview(name,chunk,preview_first,first)
                            preview_first+=self.preview_samples
                    if time.monotonic()-last_guard>=2:
                        self.ssd.check(need_bytes=FRAME_BYTES*10,require_ram=True)
                        last_guard=time.monotonic()
            if total==0:
                raise RuntimeError("No samples captured")
            if self.on_preview is not None and len(preview)>=RATE*BYTES_PER_SAMPLE:
                self.on_preview(name,bytes(preview),preview_first,first)
            self.records[name]={"speaker":name,"source":source.pulse_source,
                                "wav":str(path),"samples":total,"sample_rate":RATE,
                                "first_sample_unix_estimate":first,
                                "offset_ms":source.offset_ms,"drift_ppm":source.drift_ppm,
                                "timestamp_precision":"sample-offset estimated; not hardware word alignment"}
        except Exception as exc:
            self.errors[name]=str(exc)
            self.stop.set()
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                if proc.stdout:
                    proc.stdout.close()

    def record(self):
        validate_sources(self.sources)
        self.ssd.check(need_bytes=RATE*2*self.seconds*len(self.sources),require_ram=True)
        folder=self.ssd.directory("meetings")
        session=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+str(os.getpid())
        folder=folder/session
        folder.mkdir(mode=0o700)
        for src in self.sources:
            thread=threading.Thread(target=self._record_one,args=(src,folder),
                                    name=f"record-{src.speaker}",daemon=True)
            thread.start()
            self._threads.append(thread)
        # Give capture processes an opportunity to spawn, then release all.
        self.start.set()
        for thread in self._threads:
            thread.join(timeout=self.seconds+25)
        if any(x.is_alive() for x in self._threads):
            self.stop.set()
            for x in self._threads:
                x.join(timeout=5)
            self.errors["capture"]="One or more recorder threads timed out"
        if self.errors or len(self.records)!=len(self.sources):
            raise RuntimeError(f"Incomplete meeting capture, no MoM generated: {self.errors}")
        payload={"session":session,"created_at":iso_utc(time.time()),
                 "channels":self.records,
                 "precision_note":"USB/BT streams have independent clocks, driver buffering and latency."
                 " Offsets and drift require empirical calibration; word-exact sync is not guaranteed."}
        manifest=folder/"capture_manifest.json"
        manifest.write_text(json.dumps(payload,indent=2),encoding="utf-8")
        self.ssd.check()
        return folder,payload
