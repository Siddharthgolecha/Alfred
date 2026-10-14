"""Validated, local configuration; no secrets committed to source control."""
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT = {
    "audio": {
        "mode": "auto", "sony_name": "WI-C100", "bluetooth_address": "",
        "direct_source": "", "poll_seconds": 5, "frame_ms": 500,
        "speech_rms": 0.013, "end_silence_frames": 2, "max_utterance_seconds": 25,
        "auto_fallback": True, "probe_timeout_seconds": 5,
        "read_timeout_seconds": 5
    },
    "qwen": {
        "backend": "auto", "url": "", "binary": "",
        "model": "", "model_path": "", "expected_model": "Qwen3.5-4B",
        "ctx_size": 4096, "gpu_layers": 99, "startup_timeout_seconds": 120,
        "max_tokens": 500, "enabled": True, "supervisor_poll_seconds": 30
    },
    "runtime": {
        "database": "~/.local/share/alfred/alfred.db",
        "logs": "~/.local/state/alfred",
        "allowed_services": [], "named_tasks": {},
        "needle_confidence_floor": 0.7, "max_plan_steps": 5,
        "approval_ttl_seconds": 180,
        "tts": False, "voice_alerts": False, "telemetry": False
    }
}

def _merge(base, update):
    for key, value in update.items():
        if key not in base:
            raise ValueError(f"Unknown configuration key: {key}")
        if key == "named_tasks" and isinstance(value, dict):
            base[key] = value
        elif isinstance(base[key], dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value

def load_config(path=None):
    cfg = copy.deepcopy(DEFAULT)
    if path:
        with open(path, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    q = cfg["qwen"]
    q["model_path"] = os.environ.get("ALFRED_GGUF", q["model_path"])
    q["binary"] = os.environ.get("ALFRED_QWEN_BINARY",
                                os.environ.get("ALFRED_LLAMA_SERVER", q["binary"]))
    q["model"] = os.environ.get("ALFRED_QWEN_MODEL", q["model"])
    q["backend"] = (os.environ.get("ALFRED_QWEN_BACKEND", q["backend"])
                    .lower().replace("-", "_").replace(".", "_"))
    q["backend"] = {"lmstudio": "lm_studio", "lms": "lm_studio"}.get(q["backend"], q["backend"])
    if q["backend"] not in ("auto", "llama_cpp", "llama_server", "ollama", "lm_studio"):
        raise ValueError("qwen.backend must be auto/llama_cpp/llama_server/ollama/lm_studio")
    # A binary (or URL port) can select the provider without extra configuration.
    from .backends import backend_kind, DEFAULT_PORTS
    selected = backend_kind(q)
    explicit_url = (
        bool(path and json.loads(Path(path).read_text(encoding="utf-8")).get("qwen", {}).get("url"))
        or "ALFRED_QWEN_URL" in os.environ or "ALFRED_LLAMA_URL" in os.environ
    )
    default_url = f"http://127.0.0.1:{DEFAULT_PORTS[selected]}"
    q["url"] = os.environ.get("ALFRED_QWEN_URL",
                            os.environ.get("ALFRED_LLAMA_URL",
                                           q["url"] if explicit_url else default_url)).rstrip("/")
    parts = urlsplit(q["url"])
    if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1")
            or parts.username or parts.password or parts.port is None):
        raise ValueError("qwen.url must be a loopback HTTP origin with an explicit port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("qwen.url must be the server origin (without /v1)")
    if cfg["audio"]["mode"] not in ("auto", "direct", "bluetooth"):
        raise ValueError("audio.mode must be auto/direct/bluetooth")
    if int(cfg["audio"]["frame_ms"]) not in (250, 500, 1000):
        raise ValueError("audio.frame_ms must be 250, 500 or 1000")
    if not 1 <= float(cfg["audio"]["probe_timeout_seconds"]) <= 30:
        raise ValueError("audio.probe_timeout_seconds must be 1..30")
    if not 1 <= float(cfg["audio"]["read_timeout_seconds"]) <= 30:
        raise ValueError("audio.read_timeout_seconds must be 1..30")
    if not (0.001 <= float(cfg["audio"]["speech_rms"]) <= 0.2):
        raise ValueError("audio.speech_rms out of range")
    if not isinstance(q["model"], str) or not isinstance(q["binary"], str):
        raise ValueError("qwen.model and qwen.binary must be strings")
    if not (1 <= int(q["ctx_size"]) <= 131072):
        raise ValueError("qwen.ctx_size invalid")
    if not 10 <= int(q["supervisor_poll_seconds"]) <= 3600:
        raise ValueError("qwen.supervisor_poll_seconds must be 10..3600")
    if not 0.0 < float(cfg["runtime"]["needle_confidence_floor"]) <= 1.0:
        raise ValueError("needle_confidence_floor must be in (0,1]")
    if not 30 <= int(cfg["runtime"]["approval_ttl_seconds"]) <= 3600:
        raise ValueError("approval_ttl_seconds must be 30..3600")
    if not 1 <= int(cfg["runtime"]["max_plan_steps"]) <= 10:
        raise ValueError("max_plan_steps must be 1..10")
    return cfg

def data_path(value):
    path = Path(value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
