"""Live, privacy-respecting console display for streaming Whistle transcripts.

Nothing here persists transcript contents to disk. TTY output redraws one line;
redirected stdout emits distinct incremental updates as individual lines.
"""
from __future__ import annotations

import re
import shutil
import sys

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

def clean(text):
    raw=_ANSI.sub("",str(text or ""))
    return _CONTROL.sub(" ",raw).strip()

class TranscriptDisplay:
    """One instance per utterance, with a stable final line."""
    def __init__(self,stream=None):
        self.stream=stream if stream is not None else sys.stdout
        self.last=""
        self.live=False

    def update(self,text):
        transcript=clean(text)
        if not transcript or transcript==self.last:
            return
        self.last=transcript
        if self.stream.isatty():
            width=max(24,shutil.get_terminal_size((100,25)).columns)
            rendered="Whistle (live): "+transcript
            rendered=rendered[:width-1]
            self.stream.write("\r\x1b[2K"+rendered)
            self.stream.flush()
        else:
            self.stream.write("Whistle (live): "+transcript+"\n")
            self.stream.flush()
        self.live=True

    def finish(self,confirmed):
        final=clean(confirmed)
        if self.live and self.stream.isatty():
            self.stream.write("\r\x1b[2K")
        if final:
            self.stream.write("Whistle (final): "+final+"\n")
        elif self.live:
            self.stream.write("Whistle (final): [no confirmed speech]\n")
        self.stream.flush()
        self.last=""
        self.live=False

    def abort(self):
        if self.live:
            if self.stream.isatty():
                self.stream.write("\r\x1b[2K")
            self.stream.write("Whistle (interrupted)\n")
            self.stream.flush()
        self.last=""
        self.live=False
