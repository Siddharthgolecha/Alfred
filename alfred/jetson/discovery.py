"""Safe source discovery for a Jetson meeting with N USB/BT microphones.

A hub is a shared transport, NOT evidence of a synchronized ADC clock.
No automatic pairing, profile switching, or mute controls are performed.
"""
from __future__ import annotations

import re
import select
import subprocess
import time

from ..audio import pactl_json, source_status
from .capture import Source

MAX_MICS=8


def classify_source(entry):
    name=str(entry.get("name") or "")
    properties=entry.get("properties") or {}
    bus=str(properties.get("device.bus") or "").lower()
    if name.startswith("bluez_input."):
        return "bluetooth"
    if name.startswith("alsa_input.usb") or bus=="usb":
        return "usb"
    return None


def discover_mics(limit=MAX_MICS, probe=True, pulse_sources=None, timeout=2.0):
    """Select only functional USB/BT inputs, each from an independent source.

    A Pulse/PipeWire input source is only a *candidate* until a finite PCM
    probe succeeds. Source labels identify MIC CHANNELS, not verified people.
    """
    if type(limit) is not int or not 1<=limit<=MAX_MICS:
        raise ValueError("Automatic microphone limit must be 1..8")
    sources=pactl_json("sources") if pulse_sources is None else pulse_sources
    chosen=[]
    for item in sorted(sources,key=lambda s:str(s.get("name") or "")):
        name=str(item.get("name") or "")
        kind=classify_source(item)
        if kind is None:
            continue
        ok,_=source_status(item)
        if not ok:
            continue
        if probe and not probe_microphone(name,timeout=timeout):
            continue
        chosen.append((kind,name))
        if len(chosen)>=limit:
            break
    usb_count=0
    bt_count=0
    results=[]
    for kind,name in chosen:
        if kind=="usb":
            usb_count+=1
            label=f"USB_{usb_count}"
        else:
            bt_count+=1
            label=f"Bluetooth_{bt_count}"
        results.append(Source(label,name))
    return results


def probe_microphone(source,timeout=2.0):
    """A bounded PCM read, without storing private audio or trusting names."""
    if not source or not 0.2<=float(timeout)<=10:
        raise ValueError("Invalid audio source or probe timeout")
    proc=None
    try:
        proc=subprocess.Popen(
            ["parec","--device",source,"--raw","--format=s16le",
             "--rate","16000","--channels","1"],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
        deadline=time.monotonic()+float(timeout)
        while time.monotonic()<deadline:
            if proc.poll() is not None:
                return False
            available,_,_=select.select([proc.stdout],[],[],max(0,deadline-time.monotonic()))
            if available:
                return bool(proc.stdout.read(128))
        return False
    except (OSError,subprocess.TimeoutExpired):
        return False
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.5)
            if proc.stdout:
                proc.stdout.close
