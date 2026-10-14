"""Meeting CLI: persistent per-microphone typed rows and confirmed event timeline.

Recording is parallel; default ASR is sequential AFTER capture on 8GB Jetson.
This view is progressive ASR postprocessing, not zero-latency meeting subtitles.
"""
from __future__ import annotations

import re
import shutil
import sys
import threading

_CONTROL=re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ANSI=re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def sanitize(value):
    return _CONTROL.sub(" ",_ANSI.sub("",str(value or ""))).strip()


class MeetingDisplay:
    def __init__(self,microphones,stream=None,enabled=True):
        self.names=list(microphones)
        self.stream=sys.stdout if stream is None else stream
        self.tty=bool(enabled and getattr(self.stream,"isatty",lambda:False)())
        self.enabled=enabled
        self.lock=threading.Lock()
        self.lines={name:"" for name in self.names}
        self.counts={name:0 for name in self.names}
        if not enabled:
            return
        self.stream.write(f"Meeting transcription: {len(self.names)} microphone channels\n")
        if self.tty:
            for name in self.names:
                self.stream.write(f"[{sanitize(name)}] waiting for Whistle\n")
        self.stream.flush()

    def event(self,speaker,row):
        if not self.enabled:
            return
        name=speaker
        if name not in self.lines:
            raise ValueError("Unknown microphone channel")
        with self.lock:
            self.counts[name]+=1
            text=sanitize(row.get("text",""))
            if not text:
                return
            old=self.lines[name]
            new=(old+" "+text).strip()
            # Show recent text, not an unbounded whole-meeting line.
            new=new[-180:]
            self.lines[name]=new
            if self.tty:
                width=max(32,shutil.get_terminal_size((100,25)).columns)
                idx=self.names.index(name)
                up=len(self.names)-idx
                line=f"[{sanitize(name)}] {new}"[:width-1]
                self.stream.write(f"\x1b[{up}A\r\x1b[2K"+line+f"\x1b[{up}B\r")
            else:
                # A single confirmed segment per line. No raw partial spam.
                timestamp=row.get("start_utc","?")
                self.stream.write(f"[{sanitize(name)} @ {sanitize(timestamp)}] {text}\n")
            self.stream.flush()

    def finish(self):
        if not self.enabled:
            return
        with self.lock:
            self.stream.write("Meeting transcription completed; "
                              +", ".join(f"{k}={v} segments" for k,v in self.counts.items())
                              +"\n")
            self.stream.flush()
