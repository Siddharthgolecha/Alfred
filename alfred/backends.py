"""Local OpenAI-compatible inference services: process and model supervision.

All endpoints are loopback-only (validated by config.load_config). Never stop
or replace external services; only start a missing, known executable via argv.
"""
from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

# Import lazily to keep the existing request_json mock contract in models.py.
LOG = logging.getLogger(__name__)
DEFAULT_PORTS = {"llama_cpp": 8080, "llama_server": 8080,
                 "ollama": 11434, "lm_studio": 1234}
DEFAULT_BINARIES = {"llama_cpp": "llama", "llama_server": "llama-server",
                    "ollama": "ollama", "lm_studio": "lms"}


def backend_kind(cfg):
    """Resolve explicit type, executable/path, or well-known loopback port."""
    choice = str(cfg.get("backend", "auto")).lower().replace("-", "_").replace(".", "_")
    choice = {"lmstudio": "lm_studio", "lms": "lm_studio"}.get(choice, choice)
    if choice != "auto":
        if choice not in DEFAULT_PORTS:
            raise ValueError(f"Unsupported inference backend: {choice}")
        return choice
    binary = str(cfg.get("binary") or "").rstrip("/")
    name = Path(binary).name.lower()
    if "llama-server" in name or "llama_server" in name:
        return "llama_server"
    if name in ("ollama", "ollama.exe"):
        return "ollama"
    if name in ("lms", "lms.exe", "lmstudio", "lm-studio"):
        return "lm_studio"
    # If a URL was explicitly supplied and no executable was named, its port
    # is sufficient to select the Ollama/LM Studio adapter.
    if not binary:
        try:
            port = urlsplit(str(cfg.get("url", ""))).port
            if port == 11434:
                return "ollama"
            if port == 1234:
                return "lm_studio"
        except ValueError:
            pass
    return "llama_cpp"


def _canonical(value):
    """Normalize separators and quantization punctuation in model aliases."""
    return "".join(ch for ch in value.casefold() if ch.isalnum())


class ModelSupervisor:
    """Adopt a compatible local server or start one without shell execution."""
    def __init__(self, cfg, logdir):
        self.cfg = cfg
        self.backend = backend_kind(cfg)
        self.url = cfg["url"].rstrip("/")
        self.logdir = Path(logdir).expanduser()
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process = None
        self.started_here = False
        self._stopping = threading.Event()

    def executable_name(self):
        return self.cfg.get("binary") or DEFAULT_BINARIES[self.backend]

    def _binary(self):
        name = self.executable_name()
        path = Path(name).expanduser()
        # Accept the llama.cpp source/build directory, not just a binary.
        if path.is_dir() and self.backend in ("llama_cpp", "llama_server"):
            candidates = ["llama", "llama-server"] if self.backend == "llama_cpp" else ["llama-server"]
            for candidate in candidates:
                for location in (path / "build/bin" / candidate, path / candidate):
                    if location.is_file() and os.access(location, os.X_OK):
                        # A legacy server binary in a llama.cpp tree is valid.
                        return str(location)
            raise FileNotFoundError(f"No llama or llama-server executable in {path}/build/bin")
        resolved = shutil.which(name)
        if resolved:
            return resolved
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise FileNotFoundError(f"{self.backend} executable not found: {name}")

    def _models(self):
        from .models import request_json
        response = request_json(self.url + "/v1/models", timeout=3)
        entries = response.get("data", [])
        if not isinstance(entries, list):
            raise ValueError("Invalid /v1/models response")
        return [str(item["id"]) for item in entries if isinstance(item, dict) and item.get("id")]

    def health(self):
        from .models import request_json
        try:
            if self.backend in ("llama_cpp", "llama_server"):
                response = request_json(self.url + "/health", timeout=2)
                if response.get("status") == "ok":
                    return "ready"
                return "loading"
            self._models()
            return "ready"
        except HTTPError as exc:
            return "loading" if exc.code == 503 else "unhealthy"
        except (OSError, ValueError, URLError, KeyError, TypeError):
            return "down"

    def port_open(self):
        u = urlsplit(self.url)
        try:
            with socket.create_connection((u.hostname, u.port), timeout=1):
                return True
        except OSError:
            return False

    def model_id(self):
        """Never pick the first arbitrary model from a multi-model endpoint."""
        available = self._models()
        requested = str(self.cfg.get("model") or "").strip()
        expected = str(self.cfg.get("expected_model") or "").strip()
        if requested:
            for model in available:
                if model.casefold() == requested.casefold():
                    return model
            # llama serve -hf may publish the resolved GGUF filename instead
            # of its repo:quant reference; only accept a matching single model.
            if (self.backend in ("llama_cpp", "llama_server") and
                    len(available) == 1 and expected and
                    _canonical(expected) in _canonical(available[0]) and
                    (":" not in requested or _canonical(requested.rsplit(":", 1)[-1])
                     in _canonical(available[0]))):
                return available[0]
            raise RuntimeError(f"{self.backend}: model '{requested}' is unavailable; served models: {available}. "
                               "Install/load the model or adjust qwen.model")
        if expected:
            matches = [name for name in available if _canonical(expected) in _canonical(name)]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise RuntimeError(f"{self.backend}: expected model '{expected}' is not served; "
                                   f"available: {available}. Set qwen.model to the correct ID")
            raise RuntimeError(f"More than one model matches '{expected}'; set qwen.model explicitly")
        if len(available) == 1:
            return available[0]
        raise RuntimeError(f"Specify qwen.model (server offers {available})")

    def verify_model(self):
        return self.model_id()

    def _start_command(self, binary):
        port = str(urlsplit(self.url).port)
        model_path = str(self.cfg.get("model_path") or "").strip()
        requested = str(self.cfg.get("model") or "").strip()
        if self.backend in ("llama_cpp", "llama_server"):
            cmd = [binary]
            if Path(binary).name == "llama":
                cmd.append("serve")
            if model_path:
                file = Path(model_path).expanduser()
                if not file.is_file():
                    raise FileNotFoundError(f"Configured GGUF not found: {model_path}")
                cmd += ["--model", str(file.resolve())]
            elif requested:
                cmd += ["-hf", requested]
            else:
                raise ValueError("Set qwen.model (Hugging Face repo:quant) or qwen.model_path (GGUF)")
            cmd += ["--host", "127.0.0.1", "--port", port,
                    "--ctx-size", str(self.cfg["ctx_size"])]
            if Path(binary).name == "llama":
                cmd += ["-ngl", str(self.cfg["gpu_layers"]), "-np", "1"]
            else:
                cmd += ["--n-gpu-layers", str(self.cfg["gpu_layers"]), "--parallel", "1"]
            return cmd
        if self.backend == "ollama":
            # OLLAMA_HOST is set by ensure_running; never pull a model implicitly.
            return [binary, "serve"]
        return [binary, "server", "start", "--port", port, "--bind", "127.0.0.1"]

    def ensure_running(self):
        with self.lock:
            if self._stopping.is_set():
                raise RuntimeError("Alfred inference backend is shutting down")
            state = self.health()
            if state == "ready":
                self.verify_model()
                return True
            if self.port_open():
                if state == "loading":
                    return self._wait_ready()
                raise RuntimeError(f"{self.url} is occupied by another/unhealthy service; not replacing it")
            binary = self._binary()
            cmd = self._start_command(binary)
            log_file = self.logdir / (self.backend + ".log")
            if self.backend == "lm_studio":
                # lms server start invokes the LM Studio daemon and exits.
                # Do not treat a successful CLI exit as a failed server process.
                with open(log_file, "ab", buffering=0) as output:
                    subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT,
                                   timeout=int(self.cfg["startup_timeout_seconds"]), check=True)
                    if self.cfg.get("model"):
                        # No interactive model picker and no implicit download.
                        subprocess.run([binary, "load", self.cfg["model"], "--yes"],
                                       stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT,
                                       timeout=int(self.cfg["startup_timeout_seconds"]), check=True)
                self.started_here = True
            else:
                env = os.environ.copy()
                if self.backend == "ollama":
                    url = urlsplit(self.url)
                    env["OLLAMA_HOST"] = f"127.0.0.1:{url.port}"
                with open(log_file, "ab", buffering=0) as output:
                    self.process = subprocess.Popen(
                        cmd, env=env, stdin=subprocess.DEVNULL, stdout=output,
                        stderr=subprocess.STDOUT, start_new_session=True)
                self.started_here = True
                LOG.info("Started %s PID=%s log=%s", self.backend, self.process.pid, log_file)
            return self._wait_ready()

    def _wait_ready(self):
        end = time.monotonic() + int(self.cfg["startup_timeout_seconds"])
        while time.monotonic() < end:
            if self._stopping.is_set():
                raise RuntimeError("Alfred inference backend startup cancelled")
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"Managed {self.backend} exited with code {self.process.returncode}")
            if self.health() == "ready":
                self.verify_model()
                return True
            self._stopping.wait(0.25)
        raise TimeoutError(f"{self.backend} did not become healthy at {self.url}")

    def close(self):
        """Stop only the backend this supervisor launched, including its process group."""
        self._stopping.set()  # Wake _wait_ready before acquiring the startup lock.
        with self.lock:
            if not self.started_here:
                return  # Adopted Ollama/LM Studio/llama.cpp instances are never owned.
            process = self.process
            if process is not None:
                try:
                    if process.poll() is None:
                        # Popen(start_new_session=True) creates a private process group.
                        # A bare terminate() would leak backend worker descendants.
                        try:
                            if os.getpgid(process.pid) == process.pid:
                                os.killpg(process.pid, signal.SIGTERM)
                            else:
                                process.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            LOG.warning("%s did not terminate; killing managed process group", self.backend)
                            try:
                                if os.getpgid(process.pid) == process.pid:
                                    os.killpg(process.pid, signal.SIGKILL)
                                else:
                                    process.kill()
                            except ProcessLookupError:
                                pass
                            process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    LOG.warning("Could not fully stop managed %s: %s", self.backend, exc)
                finally:
                    self.process = None
            elif self.backend == "lm_studio":
                # LM Studio's CLI launches a daemon and exits; only undo our own start.
                try:
                    subprocess.run([self._binary(), "server", "stop"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    LOG.warning("Could not stop Alfred-started LM Studio: %s", exc)
            self.started_here = False

    def status(self):
        status = {"backend": self.backend, "url": self.url, "health": self.health(),
                  "pid": self.process.pid if self.process else None,
                  "managed": self.started_here, "model": None}
        if status["health"] == "ready":
            try:
                status["model"] = self.model_id()
            except (RuntimeError, ValueError, OSError, URLError):
                pass
        return status
