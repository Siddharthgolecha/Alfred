"""Optional best-effort LIVE meeting subtitles on a bounded ASR queue.

The live display is NOT the authoritative transcript: 8GB RAM constraints
require one Whistle worker while parallel recorders write complete WAVs to SSD.
All channels are independently re-transcribed after recording for final minutes.
"""
from __future__ import annotations

from array import array
import logging
from queue import Queue,Empty,Full
import threading
import time

from .capture import RATE,iso_utc
from .console import MeetingDisplay

LOG=logging.getLogger(__name__)


class LiveMeetingPreview:
    def __init__(self,names,whistle=None,stream=None,max_pending=8):
        if type(max_pending) is not int or not 1<=max_pending<=32:
            raise ValueError("Preview queue must be bounded to 1..32")
        self.queue=Queue(maxsize=max_pending)
        self.display=MeetingDisplay(names,stream=stream)
        self.whistle=whistle
        self.stop_flag=threading.Event()
        self.worker=threading.Thread(target=self._run,name="meeting-live-whistle",daemon=True)
        self.dropped=0
        self.errors=[]
        self.processed=0
        self.started=False

    def start(self):
        if self.started:
            raise RuntimeError("Preview worker already started")
        self.started=True
        self.worker.start()

    def submit(self,name,pcm,frame_offset,first_sample_unix):
        """Called by mic threads; never block capture or grow queue unbounded."""
        if self.stop_flag.is_set():
            return
        try:
            self.queue.put_nowait((name,pcm,frame_offset,first_sample_unix))
        except Full:
            self.dropped+=1

    def _run(self):
        try:
            whistle=self.whistle
            if whistle is None:
                import needle
                whistle=needle.Whistle()
            while not self.stop_flag.is_set() or not self.queue.empty():
                try:
                    name,raw,frame,epoch=self.queue.get(timeout=.2)
                except Empty:
                    continue
                try:
                    samples=array("h")
                    samples.frombytes(raw)
                    audio=[x/32768.0 for x in samples]
                    if not audio or max(abs(x) for x in samples)<350:
                        continue
                    if hasattr(whistle,"transcribe"):
                        result=whistle.transcribe(audio,language="en",
                            keywords=["Alfred","Jetson","Qwen"],word_timestamps=True)
                        text=str(result.get("text") or "").strip()
                    else:
                        def frames():
                            for i in range(0,len(audio),8000):
                                yield audio[i:i+8000]
                        chunks=[str(x.get("text") or "").strip()
                                for x in whistle.stream(frames(),language="en")
                                if isinstance(x,dict) and x.get("text")]
                        text=" ".join(chunks)
                    if text:
                        timestamp=iso_utc(epoch+frame/RATE)
                        self.display.event(name,{"text":text,
                                  "start_utc":timestamp,"preview":True})
                    self.processed+=1
                except Exception as exc:
                    LOG.warning("Live transcription preview error on %s: %s",name,exc)
                    self.errors.append(str(exc)[:180])
                finally:
                    self.queue.task_done()
        except Exception as exc:
            LOG.warning("Live preview model could not load: %s",exc)
            self.errors.append(str(exc)[:180])

    def close(self,timeout=30):
        self.stop_flag.set()
        if self.started:
            self.worker.join(timeout=timeout)
            if self.worker.is_alive():
                raise RuntimeError("Live preview ASR has not stopped; do not start another Whistle instance")
        self.display.finish()
        return {"processed":self.processed,"dropped":self.dropped,
                "errors":self.errors[:5],
                "note":"Live preview is best effort; post-meeting ASR is authoritative"}
