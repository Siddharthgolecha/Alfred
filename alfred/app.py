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

from .audio import AudioManager, UtteranceStream, pactl_json
from .config import data_path, load_config
from .models import Cactus, LlamaSupervisor, Qwen
from .runtime import SCHEMA_BY_NAME, TOOL_SCHEMAS, Scheduler, Store, Tools, validate_plan

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

    def reason(self,text,allow_actions=True):
        if not self.cfg["qwen"]["enabled"]:
            return {"message":"Qwen System 2 is disabled","steps":[]}
        plan = self.qwen.complete(text,list(SCHEMA_BY_NAME))
        steps = validate_plan(plan["steps"],self.cfg["runtime"]["max_plan_steps"])
        if not allow_actions:
            steps=[s for s in steps if s["tool"] in
                   ("get_system_status","get_gpu_status","list_services","list_reminders","list_schedules","list_workflows")]
        save_as = plan.get("save_as")
        if save_as:
            if not (isinstance(save_as,str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{1,40}",save_as)):
                raise ValueError("Invalid workflow name")
            if not re.search(r"\b(save|create|remember|store)\b.*\bworkflow\b",text,re.I):
                raise ValueError("Cannot save workflow without explicit user request")
            if not steps:
                raise ValueError("Cannot save an empty workflow")
            self.store.save_workflow(save_as,steps)
            self.store.event("workflow_saved",{"name":save_as,"steps":steps})
            return {"message":f"Saved workflow '{save_as}'","steps_saved":len(steps)}
        message=str(plan.get("message") or "Completed")[:700]
        results = self.tools.run_plan(steps)
        return {"message":message,"steps":len(steps),"results":results}

    def handle_text(self,text):
        text=text.strip()
        if not text:
            return
        self.store.event("utterance",{"length":len(text)})  # Do not log transcript by default.
        if self.cactus is None:
            raise RuntimeError("Cactus models were not initialized")
        response=self.cactus.route(text)
        calls=response.get("function_calls") or []
        confidence=response.get("confidence")
        if response.get("suppressed_calls") or response.get("validation",{}).get("ungrounded"):
            self.feedback.tell("I couldn't safely determine that command. Please clarify.")
            return
        floor=float(self.cfg["runtime"]["needle_confidence_floor"])
        if calls and confidence is not None and confidence < floor:
            self.feedback.tell("Command uncertain. Please repeat or be more specific.")
            return
        try:
            if not calls:
                # Unsupported direct request: reasoning may explain but cannot take action.
                self.feedback.tell(json.dumps(self.reason(text,allow_actions=False)))
                return
            for call in calls[:5]:
                name=call["name"]
                args=call.get("arguments") or {}
                if name=="delegate_to_reasoner":
                    request=args.get("request") or text
                    result=self.reason(request)
                else:
                    result=self.tools.execute(name,args)
                self.feedback.tell(json.dumps({"tool":name,"result":result},default=str)[:1400])
        except Exception as exc:
            LOG.exception("Command failed")
            self.feedback.tell(f"Could not complete command: {exc}")

    def listen(self):
        if self.cactus is None:
            raise RuntimeError("Cactus models not loaded")
        LOG.info("Starting continuous English speech recognition")
        for chunks in UtteranceStream(self.audio.frames(),self.cfg["audio"]).utterances():
            try:
                # Only dispatch finalized transcription, never provisional pending text.
                transcript=self.cactus.transcribe_stream(chunks)
                if transcript:
                    LOG.info("Utterance received (%d characters)",len(transcript))
                    self.handle_text(transcript)
                # Drain capture buffered during synchronous TTS to prevent self-trigger.
                if self.feedback.tts:
                    self.audio.close()
            except KeyboardInterrupt:
                raise
            except Exception:
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
    print("Audio input mode:",cfg["audio"]["mode"])
    try:
        for source in pactl_json("sources"):
            if "monitor" not in source.get("name","").lower():
                print("Available microphone:",source.get("name"),
                      (source.get("properties") or {}).get("device.description",""))
        for card in pactl_json("cards"):
            if str(card.get("name","")).startswith("bluez_card"):
                print("Bluetooth card:",card.get("name"),"profile:",card.get("active_profile"))
    except Exception as exc:
        print("Audio server probe failed:",exc)
    s=LlamaSupervisor(cfg["qwen"],cfg["runtime"]["logs"])
    print("llama.cpp:",json.dumps(s.status()))
    print("Configured GGUF:",cfg["qwen"]["model_path"] or "(not set)")
    print("llama-server binary:",cfg["qwen"]["binary"])

def main(argv=None):
    parser=argparse.ArgumentParser(description="Alfred Jetson voice automation agent")
    parser.add_argument("--config",default="config.json",help="Local JSON config")
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--doctor",action="store_true",help="Show mic/server status without models")
    group.add_argument("--text",help="Execute one command using Needle and tools")
    group.add_argument("--ensure-llama",action="store_true",help="Start/verify llama-server then exit")
    args=parser.parse_args(argv)
    path=Path(args.config)
    cfg=load_config(path if path.exists() else None)
    logs=Path(cfg["runtime"]["logs"]).expanduser()
    logs.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler()])
    if args.doctor:
        doctor(cfg)
        return 0
    if args.ensure_llama:
        s=LlamaSupervisor(cfg["qwen"],logs)
        s.ensure_running()
        print(json.dumps(s.status()))
        return 0
    app=Alfred(cfg)
    try:
        app.start_services()
        if args.text:
            app.handle_text(args.text)
        else:
            app.listen()
    except KeyboardInterrupt:
        LOG.info("Stopping Alfred")
    finally:
        app.close()
    return 0

if __name__=="__main__":
    raise SystemExit(main())
