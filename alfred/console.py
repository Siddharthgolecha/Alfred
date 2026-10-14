"""In-place, typing-style Whistle preview.

A terminal sees only newly arrived characters, not repeated full lines.
Partial text is display-only; it never reaches command execution.
Non-interactive output emits the FINAL transcript only, preventing log spam.
"""
from __future__ import annotations
import re
import sys

_ANSI=re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL=re.compile(r"[\x00-\x1f\x7f-\x9f]")

def clean(value):
    return _CONTROL.sub(" ",_ANSI.sub("",str(value or ""))).strip()

class TranscriptDisplay:
    """TTY incremental typing with in-place correction and one final line."""
    def __init__(self,stream=None):
        self.stream=sys.stdout if stream is None else stream
        self.last=""
        self.live=False

    def _tty(self):
        return bool(getattr(self.stream,"isatty",lambda:False)())

    def update(self,text):
        new=clean(text)
        if not new or new==self.last:
            return
        if not self._tty():
            # Piped output/systemd journals only confirmed speech, never every
            # partial hypothesis. Avoid one log line per Whistle update.
            self.last=new
            return
        if not self.live:
            self.stream.write("Whistle: ")
            self.live=True
        old=self.last
        # Appends are typed as deltas. A revised hypothesis erases the changed
        # suffix and types only the replacement. No artificial sleep/delay.
        common=0
        for a,b in zip(old,new):
            if a!=b:
                break
            common+=1
        changed=len(old)-common
        if changed:
            self.stream.write("\b"*changed)
            self.stream.write(new[common:])
            self.stream.write("\x1b[K")
        else:
            self.stream.write(new[common:])
        self.stream.flush()
        self.last=new

    def finish(self,confirmed):
        final=clean(confirmed)
        if self._tty() and self.live:
            # The final hypothesis may correct the last partial. Update only
            # the changed suffix, then finish exactly one line.
            if final:
                self.update(final)
            self.stream.write("\n")
        elif final:
            self.stream.write("Whistle: "+final+"\n")
        self.stream.flush()
        self.last=""
        self.live=False

    def discard(self):
        """Erase an unconfirmed wake hypothesis without printing bystander text."""
        if self.live and self._tty():
            self.stream.write("\r\x1b[K")
            self.stream.flush()
        self.last=""
        self.live=False

    def abort(self):
        if self.live and self._tty():
            self.stream.write("\n[Whistle interrupted]\n")
            self.stream.flush()
        self.last=""
        self.live=False
