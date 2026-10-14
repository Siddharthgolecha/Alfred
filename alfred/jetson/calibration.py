"""Optional acoustic clap calibration of independent microphone clock offsets.

Independent USB devices on the same hub are NOT sample-clock locked. Two
deliberate sharp claps (start/end) allow an approximate affine time mapping.
The output explicitly records calibration quality; failed calibration never
claims sub-sample accuracy.
"""
from __future__ import annotations

from array import array
import statistics
import wave

from .capture import RATE, iso_utc


def _peak(path,at_end=False,window_seconds=8):
    with wave.open(str(path),"rb") as wav:
        if wav.getframerate()!=RATE or wav.getsampwidth()!=2 or wav.getnchannels()!=1:
            raise ValueError("Calibration requires PCM16 mono 16 kHz WAV")
        total=wav.getnframes()
        size=min(total,RATE*window_seconds)
        begin=max(0,total-size) if at_end else 0
        wav.setpos(begin)
        raw=wav.readframes(size)
    pcm=array("h")
    pcm.frombytes(raw)
    block=RATE//100  # 10ms detector, not word-level synchronization
    if len(pcm)<block*20:
        return None
    energies=[]
    for offset in range(0,len(pcm)-block+1,block):
        values=pcm[offset:offset+block]
        energies.append(max(abs(x) for x in values))
    floor=statistics.median(energies)
    peak=max(energies)
    if peak < max(1800,5*max(floor,1)):
        return None
    i=energies.index(peak)
    # Require a prominent transient rather than a loud, sustained passage.
    nearby=energies[max(0,i-8):min(len(energies),i+9)]
    if sum(x>=peak*.65 for x in nearby)>8:
        return None
    return (begin+i*block)/RATE,round(peak/max(1,floor),2)


def calibrate_claps(records,require_two=False):
    """Return per-channel time transforms and quality assessments.

    Calibration is used ONLY when the meeting operator deliberately produced
    the same loud clap near the start and optionally at the end in every mic.
    """
    if not records:
        return {}
    names=sorted(records,key=lambda n:(not records[n]["source"].startswith("alsa_input.usb"),n))
    reference=names[0]
    anchors={}
    for name in names:
        entry=records[name]
        first=_peak(entry["wav"])
        last=_peak(entry["wav"],at_end=True)
        base=float(entry["first_sample_unix_estimate"])+float(entry.get("offset_ms",0))/1000
        anchors[name]={
            "first":(base+first[0],first[1]) if first else None,
            "last":(base+last[0],last[1]) if last else None
        }
    origin=anchors[reference]
    result={}
    for name in names:
        pair=anchors[name]
        if name==reference:
            result[name]={"method":"reference","reference":reference,
                "anchor_unix":origin["first"][0] if origin["first"] else None,
                "offset_seconds":0.0,"scale":1.0,"uncertainty_ms":80,
                "quality":"reference clock; physical capture latency unverified"}
            continue
        if not origin["first"] or not pair["first"]:
            result[name]={"method":"uncalibrated","reference":reference,
                          "scale":1.0,"offset_seconds":0.0,
                          "uncertainty_ms":None,"quality":"no common clap detected"}
            continue
        t0=pair["first"][0]
        r0=origin["first"][0]
        if abs(t0-r0)>3:
            result[name]={"method":"uncalibrated","reference":reference,
                          "scale":1.0,"offset_seconds":0.0,
                          "uncertainty_ms":None,"quality":"start anchors too far apart"}
            continue
        scale=1.0
        method="single_clap"
        if origin["last"] and pair["last"]:
            t1=pair["last"][0]
            r1=origin["last"][0]
            if t1-t0>=30 and r1-r0>=30:
                candidate=(r1-r0)/(t1-t0)
                if abs(candidate-1)<=0.002:
                    scale=candidate
                    method="two_clap_offset_and_drift"
        if require_two and method!="two_clap_offset_and_drift":
            result[name]={"method":"uncalibrated","reference":reference,
                          "scale":1.0,"offset_seconds":0.0,
                          "uncertainty_ms":None,"quality":"second reliable clap missing"}
            continue
        offset=r0-scale*t0
        result[name]={"method":method,"reference":reference,
                      "anchor_unix":t0,
                      "scale":round(scale,9),"offset_seconds":offset,
                      "uncertainty_ms":100 if method=="single_clap" else 40,
                      "quality":"approximate acoustic calibration, not hardware clock lock",
                      "drift_ppm":round((scale-1)*1e6,1)}
    return result


def adjusted_time(raw_time,calibration=None):
    """Transform an estimated time into the common reference timeline."""
    if calibration and calibration.get("method") in (
        "single_clap","two_clap_offset_and_drift"):
        return raw_time*calibration["scale"]+calibration["offset_seconds"]
    return raw_time
