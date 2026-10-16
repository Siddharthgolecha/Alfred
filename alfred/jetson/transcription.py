"""Memory-bounded, overlap-aware, speaker-channel transcription for meeting WAVs.

Whistle word timing is decoder-estimated (~80 ms frame resolution), NOT exact
ADC timestamps. Inter-channel alignment requires hardware sync or calibration.
Each independently captured mic is a SOURCE, not proof of the person's voice.
"""
from __future__ import annotations

from array import array
from difflib import SequenceMatcher
import math
import re
import wave

from .capture import Source,adjusted_seconds,iso_utc,RATE
from .calibration import adjusted_time

_CHUNK_SECONDS=12
_MIN_WORDS_DUPLICATE=3


def _make_events(words,chunk_text,first,last,entry,src,calibration,index,energy):
    speaker=src.speaker
    base=float(entry["first_sample_unix_estimate"])
    groups=[]
    if words:
        ordered=sorted(words,key=lambda w:w["start"])
        current=[]
        for word in ordered:
            if current and word["start"]-current[-1]["end"]>.65:
                groups.append(current)
                current=[]
            current.append(word)
        if current:
            groups.append(current)
    elif chunk_text:
        groups=[[]]
    out=[]
    for k,group in enumerate(groups):
        a=first/RATE+(group[0]["start"] if group else 0.0)
        z=first/RATE+(group[-1]["end"] if group else (last-first)/RATE)
        original=base+src.offset_ms/1000.0+a*(1+src.drift_ppm/1e6)
        original_end=base+src.offset_ms/1000.0+z*(1+src.drift_ppm/1e6)
        begin=adjusted_time(original,calibration)
        end=adjusted_time(original_end,calibration)
        if end<begin:
            raise ValueError("Audio timestamps out of order")
        text=" ".join(w["word"] for w in group).strip() if group else chunk_text
        if not text:
            continue
        confidence=(sum(w["probability"] for w in group)/len(group)
                    if group else None)
        structured=[]
        for word in group:
            t0=adjusted_time(base+src.offset_ms/1000+(first/RATE+word["start"])*
                             (1+src.drift_ppm/1e6),calibration)
            t1=adjusted_time(base+src.offset_ms/1000+(first/RATE+word["end"])*
                             (1+src.drift_ppm/1e6),calibration)
            structured.append({"word":word["word"],"start_seconds":round(t0,3),
                               "end_seconds":round(t1,3),"probability":word["probability"]})
        precision=("Whistle word-level estimate (~80 ms resolution)"
                   if group else "window-level, not word-level")
        out.append({
            "id":f"{speaker}-{index}-{k}","speaker":speaker,
            "source":entry["source"],"identity_type":"microphone_channel",
            "start_seconds":round(begin,3),"end_seconds":round(end,3),
            "start_utc":iso_utc(begin),"end_utc":iso_utc(end),
            "text":text,"words":structured,"confidence":confidence,
            "precision":precision,
            "sync_method":(calibration or {}).get("method","uncalibrated"),
            "synchronization_uncertainty_ms":(calibration or {}).get("uncertainty_ms"),
            "energy_rms":round(energy,5),"overlap_with":[],
            "likely_crosstalk":False,"ambiguous_crosstalk":False
        })
    return out


def _decode(wav,whistle):
    """Yield (frame_start,frame_end,text,words,energy) from bounded RAM chunks."""
    while wav.tell()<wav.getnframes():
        first=wav.tell()
        raw=wav.readframes(_CHUNK_SECONDS*RATE)
        if not raw:
            break
        last=wav.tell()
        pcm=array("h")
        pcm.frombytes(raw[:len(raw)//2*2])
        if not pcm:
            continue
        peak=max(abs(v) for v in pcm)
        if peak<350:
            continue
        # 12s PCM float window; sequential, not N copies of Whistle.
        samples=[int(v)/32768.0 for v in pcm]
        energy=math.sqrt(sum(x*x for x in samples)/len(samples))
        words=[]
        text=""
        if hasattr(whistle,"transcribe"):
            decoded=whistle.transcribe(samples,language="en",
                                      keywords=["Alfred","Jetson","Qwen"],
                                      word_timestamps=True)
            if not isinstance(decoded,dict):
                raise ValueError("Whistle.transcribe response is not a dict")
            text=str(decoded.get("text") or "").strip()
            raw_words=decoded.get("words") or []
            for value in raw_words:
                if not isinstance(value,dict):
                    continue
                token=str(value.get("word") or "").strip()
                try:
                    start=float(value["start"])
                    end=float(value["end"])
                    probability=float(value.get("probability",.5))
                except (KeyError,ValueError,TypeError):
                    continue
                if (not token or not math.isfinite(start) or not math.isfinite(end)
                    or not 0<=start<=end<=(last-first)/RATE+.2):
                    continue
                words.append({"word":token,"start":max(0,start),
                              "end":min((last-first)/RATE,end),
                              "probability":min(1.,max(0.,probability))})
        else:
            # Backward compatible with existing test/legacy engines which only
            # offer stream(); fall back honestly to coarse window timestamps.
            def frames():
                for i in range(0,len(samples),8000):
                    yield samples[i:i+8000]
            chunks=[]
            for event in whistle.stream(frames(),language="en",
                                        keywords=["Alfred","Jetson","Qwen"]):
                if isinstance(event,dict):
                    value=str(event.get("text") or "").strip()
                    if value:
                        chunks.append(value)
            text=" ".join(chunks)
        yield first,last,text,words,energy


def _normalized(text):
    return re.sub(r"\s+"," ",re.sub(r"[^\w\s]","",text.lower())).strip()


def annotate_overlap(segments):
    """Retain all raw segments; suppress only strong, time-matched duplicates.

    Common short replies and ambiguous matches are preserved instead of being
    blindly attributed to whoever had a louder mic.
    """
    for i,a in enumerate(segments):
        for b in segments[i+1:]:
            if a["speaker"]==b["speaker"]:
                continue
            intersection=min(a["end_seconds"],b["end_seconds"])-max(a["start_seconds"],b["start_seconds"])
            if intersection<=.08:
                continue
            a["overlap_with"].append(b["id"])
            b["overlap_with"].append(a["id"])
            ta=_normalized(a["text"])
            tb=_normalized(b["text"])
            if min(len(ta.split()),len(tb.split()))<_MIN_WORDS_DUPLICATE:
                continue
            duration=min(a["end_seconds"]-a["start_seconds"],
                         b["end_seconds"]-b["start_seconds"])
            if duration<=0 or intersection/duration<.68:
                continue
            if SequenceMatcher(None,ta,tb).ratio()<.94:
                continue
            # A genuine overlapping speaker may utter the same phrase.
            # Only suppress when BOTH boundaries align within decoder-scale
            # tolerances and one mic has overwhelming comparative energy.
            ea=max(1e-8,a["energy_rms"])
            eb=max(1e-8,b["energy_rms"])
            aligned=(abs(a["start_seconds"]-b["start_seconds"])<=.16
                     and abs(a["end_seconds"]-b["end_seconds"])<=.26)
            if aligned and max(ea,eb)/min(ea,eb)>=3:
                weaker=a if ea<eb else b
                weaker["likely_crosstalk"]=True
            else:
                a["ambiguous_crosstalk"]=True
                b["ambiguous_crosstalk"]=True
    for row in segments:
        row["overlap"]=bool(row["overlap_with"])
        row["included_in_minutes"]=not row["likely_crosstalk"]
    return segments


def merge_channels(records,whistle=None,calibrations=None,on_event=None,
                   include_crosstalk=False):
    """Transcribe each microphone on one model and merge by calibrated word time.

    Returned events preserve overlap, word timing, and explicit uncertainty.
    on_event receives each per-mic raw event as transcription progresses.
    """
    if whistle is None:
        import needle
        whistle=needle.Whistle()
    calibrations=calibrations or {}
    events=[]
    for name,entry in sorted(records.items()):
        src=Source(name,entry["source"],entry.get("offset_ms",0),
                   entry.get("drift_ppm",0))
        with wave.open(entry["wav"],"rb") as wav:
            if wav.getframerate()!=RATE or wav.getnchannels()!=1 or wav.getsampwidth()!=2:
                raise ValueError("Captured WAV must be PCM16 mono 16kHz")
            for chunk,(first,last,text,words,energy) in enumerate(_decode(wav,whistle)):
                candidates=_make_events(words,text,first,last,entry,src,
                                        calibrations.get(name),chunk,energy)
                for e in candidates:
                    events.append(e)
                    if on_event is not None:
                        on_event(name,e)
    events.sort(key=lambda e:(e["start_seconds"],e["speaker"],e["id"]))
    annotate_overlap(events)
    return events if include_crosstalk else [e for e in events if e["included_in_minutes"]]


def pretty_transcript(segments):
    lines=[]
    for e in segments:
        marker=" [OVERLAP]" if e.get("overlap") else ""
        caution=" [POSSIBLE CROSS-TALK]" if e.get("ambiguous_crosstalk") else ""
        lines.append(f"[{e['start_utc']} .. {e['end_utc']}] "
                     f"{e['speaker']} ({e.get('source','mic')}){marker}{caution}: {e['text']}")
    return "\n".join(lines)
