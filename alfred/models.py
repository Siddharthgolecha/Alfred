"""Cactus Needle/Whistle and managed local llama.cpp Qwen service."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)

def request_json(url, data=None, timeout=4):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)

class LlamaSupervisor:
    """Adopt a healthy existing server; otherwise start exactly one local process.

    Never kill an external server or overwrite an occupied port.
    """
    def __init__(self, cfg, logdir):
        self.cfg = cfg
        self.url = cfg["url"].rstrip("/")
        self.logdir = Path(logdir).expanduser()
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process = None
        self.started_here = False

    def health(self):
        try:
            response = request_json(self.url + "/health", timeout=2)
            if response.get("status") == "ok":
                return "ready"
            return "loading"
        except HTTPError as exc:
            if exc.code == 503:
                return "loading"
            return "unhealthy"
        except (OSError, ValueError, URLError):
            return "down"

    def port_open(self):
        u = urlsplit(self.url)
        try:
            with socket.create_connection((u.hostname, u.port or 80), timeout=1):
                return True
        except OSError:
            return False

    def model_id(self):
        try:
            data = request_json(self.url + "/v1/models")
            models = data.get("data", [])
            return str(models[0].get("id", "")) if models else ""
        except (OSError, ValueError, LookupError):
            return ""

    def verify_model(self):
        expected = self.cfg.get("expected_model", "").lower()
        actual = self.model_id()
        if expected and actual and expected not in actual.lower():
            raise RuntimeError(f"Existing llama-server serves '{actual}', expected '{expected}'; will not replace it")
        return actual

    def ensure_running(self):
        with self.lock:
            status = self.health()
            if status == "ready":
                self.verify_model()
                return True
            if self.port_open():
                # A 503 loading server can finish booting; do not launch another.
                if status == "loading":
                    return self._wait_ready()
                raise RuntimeError(f"Port for {self.url} is occupied by an unhealthy/other service")
            model = Path(self.cfg["model_path"]).expanduser()
            if not str(self.cfg["model_path"]).strip() or not model.is_file():
                raise FileNotFoundError("Configure qwen.model_path or ALFRED_GGUF to an existing Bartowski Q4_K_M GGUF")
            binary = self.cfg["binary"]
            binary_resolved = shutil.which(binary) or (binary if Path(binary).is_file() else None)
            if not binary_resolved:
                raise FileNotFoundError(f"llama-server executable not found: {binary}")
            u = urlsplit(self.url)
            cmd = [binary_resolved, "--model", str(model.resolve()), "--host", "127.0.0.1",
                   "--port", str(u.port or 80), "--ctx-size", str(self.cfg["ctx_size"]),
                   "--n-gpu-layers", str(self.cfg["gpu_layers"]), "--parallel", "1"]
            log_file = self.logdir / "llama-server.log"
            with open(log_file, "ab", buffering=0) as stream:
                self.process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                                stdout=stream, stderr=subprocess.STDOUT,
                                                start_new_session=True)
            self.started_here = True
            LOG.info("Started llama-server PID %s, log=%s", self.process.pid, log_file)
            return self._wait_ready()

    def _wait_ready(self):
        end = time.monotonic() + int(self.cfg["startup_timeout_seconds"])
        while time.monotonic() < end:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"Managed llama-server exited with code {self.process.returncode}")
            if self.health() == "ready":
                self.verify_model()
                return True
            time.sleep(1)
        raise TimeoutError(f"llama-server did not become healthy at {self.url}")

    def status(self):
        return {"health": self.health(), "model": self.model_id(),
                "pid": self.process.pid if self.process else None,
                "managed": self.started_here}

class Qwen:
    def __init__(self, cfg, supervisor):
        self.cfg = cfg
        self.supervisor = supervisor

    def complete(self, transcript, tool_names):
        self.supervisor.ensure_running()
        model = self.supervisor.model_id()
        prompt = (
            "You are Alfred's local System 2 planner. Reply ONLY with JSON: "
            '{"message":"short explanation","steps":[{"tool":"tool_name","args":{}}]}. '
            "Use zero steps for a question with no necessary tools. "
            "For actions use only exact tools from this list: " + ", ".join(tool_names) + ". "
            "Arguments must match the tool schemas implemented by Python. "
            "Never guess service names, task names, or reminder ids; ask for clarification in message. "
            "Never produce shell commands or Python code. Maximum five steps. "
            "Examples: {\"message\":\"Checking status\",\"steps\":[{\"tool\":\"get_system_status\",\"args\":{}}]}. "
            "For conditional tasks requiring persistent monitoring, say the feature is unsupported "
            "unless a declared scheduling tool can express it."
        )
        payload = {
            "messages": [{"role":"system","content":prompt},{"role":"user","content":transcript}],
            "temperature": 0.1, "max_tokens": int(self.cfg["max_tokens"]),
            "response_format": {"type":"json_object"}, "stream": False
        }
        if model:
            payload["model"] = model
        out = request_json(self.cfg["url"].rstrip("/") + "/v1/chat/completions",
                           payload, timeout=90)
        choice = out["choices"][0]["message"]
        content = choice.get("content") or ""
        try:
            plan = json.loads(content)
        except ValueError as exc:
            raise ValueError("Qwen did not return valid JSON") from exc
        if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
            raise ValueError("Qwen response missing steps array")
        return plan

class Cactus:
    """One long-lived Needle instance; native inference kept on calling thread."""
    def __init__(self, schemas):
        import needle
        self.needle = needle
        self.agent = needle.Needle(tools=schemas)
        self.whistle = needle.Whistle()

    def route(self, transcript):
        self.agent.reset()
        return self.agent.complete(transcript, max_new_tokens=384)

    def transcribe_stream(self, frames, on_partial=None):
        # Whistle commits segments as it receives streaming samples.
        pieces = []
        pending = ""
        for event in self.whistle.stream(frames, language="en", keywords=["Alfred", "Jetson", "Sony"]):
            committed = str(event.get("text") or "").strip()
            pending = str(event.get("pending") or "")
            if committed:
                pieces.append(committed)
            if on_partial:
                on_partial(" ".join(pieces + ([pending] if pending else [])))
        return " ".join(pieces).strip()
