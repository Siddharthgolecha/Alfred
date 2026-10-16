"""Local OpenAI-compatible inference services: process and model supervision.

All endpoints are loopback-only (validated by config.load_config). Never stop
or replace external services; only start a missing, known executable via argv.
"""
from __future__ import annotations

import ipaddress
import json
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

from .platform_utils import terminate_process_group, kill_process_group, IS_WINDOWS

# Import lazily to keep the existing request_json mock contract in models.py.
LOG = logging.getLogger(__name__)

# Module-level sentinel port constants
PORT_LLAMA_CPP = 8080
PORT_LLAMA_SERVER = 8080
PORT_OLLAMA = 11434
PORT_LM_STUDIO = 1234

DEFAULT_PORTS = {
    "llama_cpp": PORT_LLAMA_CPP,
    "llama_server": PORT_LLAMA_SERVER,
    "ollama": PORT_OLLAMA,
    "lm_studio": PORT_LM_STUDIO,
}
DEFAULT_BINARIES = {
    "llama_cpp": "llama",
    "llama_server": "llama-server",
    "ollama": "ollama",
    "lm_studio": "lms",
}


def _lookup_backend_by_binary_or_port(binary: str, url: str) -> str | None:
    """Resolve a well-known backend by its binary name or url port.

    Returns None when neither identifies a known backend; callers fall back to llama_cpp.
    """
    if binary:
        name = Path(binary).name.lower()
        if "llama-server" in name or "llama_server" in name:
            return "llama_server"
        if name in ("ollama", "ollama.exe"):
            return "ollama"
        if name in ("lms", "lms.exe", "lmstudio", "lm-studio"):
            return "lm_studio"
    else:
        try:
            url_parts = urlsplit(url)
            port = url_parts.port
            if port == PORT_OLLAMA:
                return "ollama"
            if port == PORT_LM_STUDIO:
                return "lm_studio"
        except ValueError:
            pass
    return None


def backend_kind(config):
    """Resolve explicit type, executable/path, or well-known loopback port."""
    choice = str(config.get("backend", "auto")).lower().replace("-", "_").replace(".", "_")
    choice = {"lmstudio": "lm_studio", "lms": "lm_studio"}.get(choice, choice)
    if choice != "auto":
        if choice not in DEFAULT_PORTS:
            raise ValueError(f"Unsupported inference backend: {choice}")
        return choice
    binary = str(config.get("binary") or "").rstrip("/")
    resolved = _lookup_backend_by_binary_or_port(binary, str(config.get("url", "")))
    if resolved is not None:
        return resolved
    return "llama_cpp"


def _canonical(value):
    """Normalize separators and quantization punctuation in model aliases."""
    return "".join(character for character in value.casefold() if character.isalnum())


class ModelSupervisor:
    """Adopt a compatible local server or start one without shell execution."""
    def __init__(self, config, logdir):
        self.config = config
        self.backend = backend_kind(config)
        self.url = config["url"].rstrip("/")
        self.logdir = Path(logdir).expanduser()
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process = None
        self.started_here = False
        self._stopping = threading.Event()
        self._ollama_model_preloaded = False

    def executable_name(self):
        return self.config.get("binary") or DEFAULT_BINARIES[self.backend]

    def _resolve_binary(self) -> str:
        """Resolve and validate the path to the backend executable."""
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

    def health(self) -> str:
        from .models import request_json
        try:
            if self.backend in ("llama_cpp", "llama_server"):
                response = request_json(self.url + "/health", timeout=2)
                if response.get("status") == "ok":
                    return "ready"
                return "loading"
            self._models()
            return "ready"
        except HTTPError as error:
            return "loading" if error.code == 503 else "unhealthy"
        except (OSError, ValueError):
            return "down"

    def port_open(self):
        url_parts = urlsplit(self.url)
        try:
            with socket.create_connection((url_parts.hostname, url_parts.port), timeout=1):
                return True
        except OSError:
            return False

    def model_id(self):
        """Never pick the first arbitrary model from a multi-model endpoint."""
        available = self._models()
        requested = str(self.config.get("model") or "").strip()
        expected = str(self.config.get("expected_model") or "").strip()
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

    def _start_command(self, binary: str) -> list[str]:
        port = str(urlsplit(self.url).port)
        model_path = str(self.config.get("model_path") or "").strip()
        requested = str(self.config.get("model") or "").strip()
        if self.backend in ("llama_cpp", "llama_server"):
            command = [binary]
            if Path(binary).name == "llama":
                command.append("serve")
            if model_path:
                file = Path(model_path).expanduser()
                if not file.is_file():
                    raise FileNotFoundError(f"Configured GGUF not found: {model_path}")
                command += ["--model", str(file.resolve())]
            elif requested:
                command += ["-hf", requested]
            else:
                raise ValueError("Set qwen.model (Hugging Face repo:quant) or qwen.model_path (GGUF)")
            command += ["--host", "127.0.0.1", "--port", port,
                        "--ctx-size", str(self.config["ctx_size"])]
            if Path(binary).name == "llama":
                command += ["-ngl", str(self.config["gpu_layers"]), "-np", "1"]
            else:
                command += ["--n-gpu-layers", str(self.config["gpu_layers"]), "--parallel", "1"]
            return command
        if self.backend == "ollama":
            # OLLAMA_HOST is set by ensure_running; never pull a model implicitly.
            return [binary, "serve"]
        return [binary, "server", "start", "--port", port, "--bind", "127.0.0.1"]

    def _check_health(self) -> bool:
        """Check if the backend is already ready or loading on the target port."""
        state = self.health()
        if state == "ready":
            self.verify_model()
            return True
        if self.port_open():
            if state == "loading":
                return self._wait_ready()
            raise RuntimeError(f"{self.url} is occupied by another/unhealthy service; not replacing it")
        return False

    def _is_loopback_endpoint(self) -> bool:
        host = urlsplit(self.url).hostname
        if not host:
            return False
        if host.casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    def _lm_studio_loaded_ids(self, binary: str) -> set[str]:
        result = subprocess.run([binary, "ps", "--json"], capture_output=True,
                                text=True, timeout=10, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown CLI error").strip()
            raise RuntimeError("Could not inspect loaded LM Studio models: " + detail)
        try:
            rows = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("LM Studio returned invalid JSON for loaded models") from exc
        if not isinstance(rows, list):
            raise RuntimeError("LM Studio returned an unexpected loaded-model list")
        identifiers = set()
        for row in rows:
            if isinstance(row, dict):
                for key in ("identifier", "modelKey", "model_key", "path",
                            "modelPath", "model_path"):
                    value = row.get(key)
                    if isinstance(value, str) and value.strip():
                        identifiers.add(value.strip().casefold())
        return identifiers

    def _ensure_lm_studio_model_loaded(self) -> None:
        """Load the explicitly configured model on an adopted local LM server.

        LM Studio's OpenAI model listing includes installed models that are not
        resident, so ``verify_model`` alone cannot prove that inference can run.
        Never use a local ``lms`` CLI to control a remote endpoint.
        """
        if self.backend != "lm_studio" or not self._is_loopback_endpoint():
            return
        requested = str(self.config.get("model") or "").strip()
        if not requested:
            return
        binary = self._resolve_binary()
        if requested.casefold() in self._lm_studio_loaded_ids(binary):
            return
        LOG.info("Loading configured LM Studio model %s", requested)
        result = subprocess.run([binary, "load", requested, "--yes"],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True,
                                timeout=int(self.config["startup_timeout_seconds"]),
                                check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown CLI error").strip()
            raise RuntimeError(f"Could not load configured LM Studio model '{requested}': "
                               + detail)
        loaded = self._lm_studio_loaded_ids(binary)
        if requested.casefold() not in loaded:
            raise RuntimeError(f"LM Studio load command completed, but '{requested}' "
                               "is not listed as loaded")
        LOG.info("Configured LM Studio model is loaded: %s", requested)

    def _ollama_loaded_ids(self) -> set[str]:
        from .models import request_json
        response = request_json(self.url + "/api/ps", timeout=5)
        if not isinstance(response, dict) or not isinstance(response.get("models"), list):
            raise RuntimeError("Ollama returned an unexpected /api/ps response")
        identifiers = set()
        for row in response["models"]:
            if isinstance(row, dict):
                for key in ("name", "model"):
                    value = row.get(key)
                    if isinstance(value, str) and value.strip():
                        identifiers.add(value.strip().casefold())
        return identifiers

    def _ensure_ollama_model_loaded(self) -> None:
        """Preload the selected local Ollama model once for this Alfred session.

        Ollama's OpenAI-compatible model list includes installed models, while
        ``/api/ps`` reports models resident in memory. An empty chat request
        loads the selected model without generating user-facing text; Ollama's
        own default keep-alive then manages idle memory reclamation.
        """
        if self.backend != "ollama" or not self._is_loopback_endpoint():
            return
        requested = str(self.config.get("model") or "").strip()
        if not requested:
            return
        loaded = self._ollama_loaded_ids()
        if requested.casefold() in loaded:
            self._ollama_model_preloaded = True
            return
        # Avoid undoing Ollama's default idle-unload behavior on every watchdog
        # poll. The first ensure warms the model; later inference calls let the
        # backend reload it naturally after an idle timeout if needed.
        if self._ollama_model_preloaded:
            return
        from .models import request_json
        result = request_json(self.url + "/api/chat", {
            "model": requested, "messages": [], "stream": False
        }, timeout=int(self.config["startup_timeout_seconds"]))
        if not isinstance(result, dict) or result.get("done") is not True:
            raise RuntimeError(f"Ollama did not finish loading configured model '{requested}'")
        if requested.casefold() not in self._ollama_loaded_ids():
            raise RuntimeError(f"Ollama completed its load request, but '{requested}' "
                               "is not listed as loaded")
        self._ollama_model_preloaded = True
        LOG.info("Configured Ollama model is loaded: %s", requested)

    def _ensure_configured_model_loaded(self) -> None:
        if self.backend == "lm_studio":
            self._ensure_lm_studio_model_loaded()
        elif self.backend == "ollama":
            self._ensure_ollama_model_loaded()

    def _start_lm_studio_daemon(self, command: list[str], binary: str, log_file: Path) -> None:
        """Launch the LM Studio daemon and optionally load the requested model."""
        startup_timeout = int(self.config["startup_timeout_seconds"])
        with open(log_file, "ab", buffering=0) as output:
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=startup_timeout,
                check=True,
            )
            if self.config.get("model"):
                # No interactive model picker and no implicit download.
                subprocess.run(
                    [binary, "load", self.config["model"], "--yes"],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=startup_timeout,
                    check=True,
                )
        self.started_here = True

    def _start_background_service(self, command: list[str], log_file: Path) -> None:
        """Launch Ollama or llama-server/llama.cpp as a background process group."""
        environment = os.environ.copy()
        if self.backend == "ollama":
            url_parts = urlsplit(self.url)
            environment["OLLAMA_HOST"] = f"127.0.0.1:{url_parts.port}"
        popen_kwargs: dict = dict(
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=None,  # set below
            stderr=subprocess.STDOUT,
        )
        # start_new_session creates a private process group on POSIX so that
        # SIGTERM/SIGKILL can target the whole group. Windows uses
        # CREATE_NEW_PROCESS_GROUP instead (via creationflags).
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        with open(log_file, "ab", buffering=0) as output:
            popen_kwargs["stdout"] = output
            self.process = subprocess.Popen(command, **popen_kwargs)
        self.started_here = True
        LOG.info("Started %s PID=%s log=%s", self.backend, self.process.pid, log_file)

    def _start_process(self, binary: str) -> None:
        """Start the backend process, configuring logging, environment, and CLI flags."""
        command = self._start_command(binary)
        log_file = self.logdir / f"{self.backend}.log"
        if self.backend == "lm_studio":
            self._start_lm_studio_daemon(command, binary, log_file)
        else:
            self._start_background_service(command, log_file)

    def ensure_running(self) -> bool:
        """Ensure the backend is running, adopting any active instance or starting a new one."""
        with self.lock:
            if self._stopping.is_set():
                raise RuntimeError("Alfred inference backend is shutting down")
            if self._check_health():
                self._ensure_configured_model_loaded()
                return True
            binary = self._resolve_binary()
            self._start_process(binary)
            ready = self._wait_ready()
            self._ensure_configured_model_loaded()
            return ready

    def _wait_ready(self) -> bool:
        end_time = time.monotonic() + int(self.config["startup_timeout_seconds"])
        while time.monotonic() < end_time:
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
                        # Use cross-platform helpers to signal the process group.
                        terminate_process_group(process)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            LOG.warning("%s did not terminate; killing managed process group", self.backend)
                            kill_process_group(process)
                            process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as error:
                    LOG.warning("Could not fully stop managed %s: %s", self.backend, error)
                finally:
                    self.process = None
            elif self.backend == "lm_studio":
                # LM Studio's CLI launches a daemon and exits; only undo our own start.
                try:
                    subprocess.run([self._resolve_binary(), "server", "stop"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired) as error:
                    LOG.warning("Could not stop Alfred-started LM Studio: %s", error)
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
