"""Alfred CLI and always-on voice orchestrator."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from .audio import AudioManager, AudioUnavailableError, UtteranceStream, pactl_json
from .console import TranscriptDisplay
from .routing import adjust_status_calls
from .config import data_path, load_config
from .models import Cactus, LlamaSupervisor, Qwen
from .runtime import SCHEMA_BY_NAME, TOOL_SCHEMAS, Scheduler, Store, Tools
from .system2 import System2

LOG = logging.getLogger(__name__)

class Feedback:
    def __init__(self,enable_tts=False):
        self.tts = enable_tts
        self.speaking = threading.Event()

    def tell(self,message):
        print(message,flush=True)
        if self.tts and shutil.which("spd-say"):
            self.speaking.set()
            try:
                subprocess.run(["spd-say","-w",str(message)[:300]],timeout=25,check=False)
            except (OSError,subprocess.TimeoutExpired) as exc:
                LOG.warning("Speech output failed: %s",exc)
            finally:
                self.speaking.clear()

class Alfred:
    def __init__(self,cfg,load_models=True):
        self.cfg = cfg
        runtime = cfg["runtime"]
        self.store = Store(runtime["database"])
        self.tools = Tools(runtime,self.store)
        self.feedback = Feedback(runtime["tts"])
        self.audio = AudioManager(cfg["audio"])
        self.supervisor = LlamaSupervisor(cfg["qwen"],runtime["logs"])
        self.qwen = Qwen(cfg["qwen"],self.supervisor)
        self.system2 = System2(self.qwen,self.tools,self.store,runtime)
        if not runtime.get("telemetry",False):
            import os
            os.environ.setdefault("NEEDLE_TELEMETRY","0")
        self.cactus = Cactus(TOOL_SCHEMAS) if load_models else None
        self.scheduler = Scheduler(self.store,self.tools,self._remind)
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None

    def _remind(self,event):
        text=f"Reminder #{event['id']}: {event['message']}"
        self.feedback.tell(text)
        self.store.event("reminder",event)

    def start_services(self):
        self.scheduler.start()
        if self.cfg["qwen"]["enabled"]:
            def watchdog():
                while not self._watchdog_stop.is_set():
                    try:
                        self.supervisor.ensure_running()
                        LOG.info("Qwen available: %s", self.supervisor.status())
                    except Exception as exc:
                        LOG.warning("Qwen unavailable; will retry: %s", exc)
                    self._watchdog_stop.wait(int(self.cfg["qwen"]["supervisor_poll_seconds"]))
            self._watchdog_thread = threading.Thread(
                target=watchdog, name="llama-supervisor", daemon=True)
            self._watchdog_thread.start()

    def reason(self, text, allow_actions=True):
        if not self.cfg["qwen"]["enabled"]:
            return {"message":"Qwen System 2 is disabled","results":[]}
        return self.system2.reason(text,allow_actions=allow_actions)

    def handle_text(self,text):
        text=text.strip()
        if not text:
            return
        self.store.event("utterance",{"length":len(text)})  # No raw transcript stored.
        if self.cactus is None:
            raise RuntimeError("Cactus models were not initialized")
        try:
            response=self.cactus.route(text)
            calls=response.get("function_calls") or []
            confidence=response.get("confidence")
            validation = response.get("validation") or {}
            if response.get("suppressed_calls") or (
                isinstance(validation, dict) and validation.get("ungrounded")
            ):
                self.feedback.tell("I couldn't safely determine that command. Please clarify.")
                return
            floor=float(self.cfg["runtime"]["needle_confidence_floor"])
            if calls and (confidence is None or not isinstance(confidence,(float,int))
                          or confidence < floor):
                self.store.event("route_uncertain",{
                    "confidence": confidence, "call_count": len(calls)})
                self.feedback.tell("Command uncertain. Please repeat or be more specific.")
                return
            corrected=adjust_status_calls(text,calls)
            if corrected is not calls:
                self.store.event("routing_adjustment",{
                    "reason":"explicit_gpu_status",
                    "original":[c.get("name") for c in calls],
                    "selected":[c.get("name") for c in corrected]
                })
                calls=corrected
            self.store.event("route", {
                "model":"needle3", "confidence":confidence,
                "call_count":len(calls),
                "delegated":bool(not calls or (len(calls)==1 and
                    calls[0].get("name")=="delegate_to_reasoner"))
            })
            if not calls:
                # An unsupported Needle command may still be an ordinary Q&A question.
                # Qwen here is strictly read-only, regardless of what it proposes.
                result=self.reason(text,allow_actions=False)
            elif len(calls)==1 and calls[0].get("name")=="delegate_to_reasoner":
                # Use the actual transcript, never a possibly altered model argument.
                result=self.reason(text,allow_actions=True)
            else:
                result=self.system2.direct(calls)
            if "approval" in result:
                approval=result["approval"]
                message=(f"Local approval required for {approval['actions']}. "
                         f"On the Jetson, run: alfred --approve {approval['approval_id']}")
                self.feedback.tell(message)
            else:
                self.feedback.tell(json.dumps(result,default=str)[:1800])
        except Exception as exc:
            LOG.exception("Command failed")
            self.feedback.tell(f"Could not complete command: {exc}")

    def listen(self):
        if self.cactus is None:
            raise RuntimeError("Cactus models not loaded")
        LOG.info("Starting continuous English speech recognition")
        for chunks in UtteranceStream(self.audio.frames(),self.cfg["audio"]).utterances():
            transcript_display=TranscriptDisplay()
            try:
                # Show partial text immediately, but execute only finalized words.
                transcript=self.cactus.transcribe_stream(
                    chunks,on_partial=transcript_display.update)
                transcript_display.finish(transcript)
                if transcript:
                    LOG.info("Utterance received (%d characters)",len(transcript))
                    self.handle_text(transcript)
                # Drain capture buffered during synchronous TTS to prevent self-trigger.
                if self.feedback.tts:
                    self.audio.close()
            except (KeyboardInterrupt, AudioUnavailableError):
                transcript_display.abort()
                raise
            except Exception:
                transcript_display.abort()
                LOG.exception("Audio/transcription error, recovering")
                self.audio.close()
                time.sleep(1)

    def close(self):
        self._watchdog_stop.set()
        self.audio.close()
        self.scheduler.stop()
        # A llama-server started by Alfred is deliberately kept alive; next
        # Alfred start reuses its /health endpoint instead of spawning a duplicate.

def doctor(cfg):
    """Make advertised devices and *verified live microphone* visibly distinct."""
    print("Audio input mode:",cfg["audio"]["mode"])
    manager=AudioManager(cfg["audio"])
    healthy=False
    try:
        entries=manager.diagnostics()
        if not entries:
            print("Audio server lists no recording sources.")
        for source in entries:
            tag="CANDIDATE" if source["eligible"] else "REJECTED"
            print(f"[{tag}] {source['description'] or source['name']}: "
                  f"{source['name']} — {source['reason']}")
        try:
            selected=manager.ensure_input()
            print(f"MICROPHONE INPUT STREAM VERIFIED: {selected}")
            healthy=True
        except AudioUnavailableError as exc:
            print(f"MICROPHONE NOT CONNECTED / NOT USABLE: {exc}")
    except Exception as exc:
        print(f"MICROPHONE NOT CONNECTED / AUDIO SERVER FAILURE: {exc}")
    finally:
        manager.close()
    try:
        for card in pactl_json("cards"):
            if str(card.get("name","")).startswith("bluez_card"):
                print("Bluetooth card:",card.get("name"),"profile:",card.get("active_profile"))
    except (OSError,ValueError,RuntimeError) as exc:
        print("Bluetooth profile detection unavailable:",exc)
    s=LlamaSupervisor(cfg["qwen"],cfg["runtime"]["logs"])
    print("llama.cpp:",json.dumps(s.status()))
    print("Configured GGUF:",cfg["qwen"]["model_path"] or "(not set)")
    print("llama-server binary:",cfg["qwen"]["binary"])
    return healthy

def main(argv=None):
    parser=argparse.ArgumentParser(description="Alfred Jetson voice automation agent")
    parser.add_argument("--config",default="config.json",help="Local JSON config")
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--doctor",action="store_true",help="Show mic/server status without models")
    group.add_argument("--text",help="Execute one command using Needle and tools")
    group.add_argument("--ensure-llama",action="store_true",help="Start/verify llama-server then exit")
    group.add_argument("--reason",help="Test Qwen planner on a text input; no Cactus engine required")
    group.add_argument("--pending",action="store_true",help="List proposed tool actions awaiting approval")
    group.add_argument("--approve",type=int,metavar="ID",help="Locally approve and execute a proposal once")
    group.add_argument("--reject",type=int,metavar="ID",help="Locally reject a proposal")
    args=parser.parse_args(argv)
    path=Path(args.config)
    cfg=load_config(path if path.exists() else None)
    logs=Path(cfg["runtime"]["logs"]).expanduser()
    logs.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler()])
    if args.doctor:
        return 0 if doctor(cfg) else 2
    if args.ensure_llama:
        s=LlamaSupervisor(cfg["qwen"],logs)
        s.ensure_running()
        print(json.dumps(s.status()))
        return 0
    # Administrative actions are intentionally CLI-only; they never appear in
    # Needle's tool schemas. No model needs loading to approve or reject.
    if args.pending or args.approve is not None or args.reject is not None:
        app=Alfred(cfg,load_models=False)
        try:
            if args.pending:
                print(json.dumps(app.system2.approvals.pending(),indent=2))
            elif args.approve is not None:
                print(json.dumps(app.system2.approve(args.approve),default=str,indent=2))
            else:
                print(json.dumps({"rejected":app.system2.approvals.reject(args.reject)}))
        finally:
            app.close()
        return 0
    if args.reason:
        app=Alfred(cfg,load_models=False)
        try:
            print(json.dumps(app.reason(args.reason),default=str,indent=2))
        finally:
            app.close()
        return 0
    if not args.text:
        # Fail before loading native Whistle/Needle models or starting services.
        # Merely seeing a Pulse "Built-in Audio" entry is not enough.
        mic=AudioManager(cfg["audio"])
        try:
            selected=mic.ensure_input()
            LOG.info("Audio preflight passed: %s",selected)
        except AudioUnavailableError as exc:
            LOG.error("MICROPHONE NOT CONNECTED / NOT USABLE: %s",exc)
            return 2
        finally:
            mic.close()
    app=Alfred(cfg)
    try:
        app.start_services()
        if args.text:
            app.handle_text(args.text)
        else:
            app.listen()
    except KeyboardInterrupt:
        LOG.info("Stopping Alfred")
    except AudioUnavailableError as exc:
        LOG.error("Microphone disconnected or capture failed: %s",exc)
        return 2
    finally:
        app.close()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
