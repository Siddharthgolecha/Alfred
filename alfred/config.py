"""Validated, local configuration; no secrets committed to source control."""
import copy
import json
import os
import re
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
    "wake_word": {
        "enabled": True, "phrase": "Alfred", "mode": "transcript",
        "followup_seconds": 8, "model_path": "", "threshold": 0.75
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
        "tts": False, "voice_alerts": False, "telemetry": False,
        "cognitive_diagnostics": False,
        "harness_enabled": False, "harness_timeout_seconds": 45,
        "needle_loop_enabled": False, "context_enabled": False,
        "context_persistence_enabled": False,
        "context_owner": "local", "context_mode": "assistant",
        "context_ttl_seconds": 120, "context_budget_chars": 1200,
        "needle_max_decisions": 3,
        "needle_max_tools": 3, "needle_context_ttl_seconds": 120
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
    wake = cfg["wake_word"]
    if not isinstance(wake["enabled"], bool):
        raise ValueError("wake_word.enabled must be a boolean")
    if wake["mode"] not in ("transcript", "openwakeword"):
        raise ValueError("wake_word.mode must be transcript or openwakeword")
    if (not isinstance(wake["phrase"], str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{0,39}", wake["phrase"])
            or wake["phrase"] != wake["phrase"].strip()):
        raise ValueError("wake_word.phrase must be a nonempty literal word/phrase")
    if (not isinstance(wake["followup_seconds"], (int, float))
            or not 0 <= wake["followup_seconds"] <= 30):
        raise ValueError("wake_word.followup_seconds must be 0..30")
    if (not isinstance(wake["threshold"], (int, float))
            or not 0 < wake["threshold"] <= 1):
        raise ValueError("wake_word.threshold must be in (0,1]")
    if not isinstance(wake["model_path"], str):
        raise ValueError("wake_word.model_path must be a local path string")
    if not isinstance(q["model"], str) or not isinstance(q["binary"], str):
        raise ValueError("qwen.model and qwen.binary must be strings")
    if not (1 <= int(q["ctx_size"]) <= 131072):
        raise ValueError("qwen.ctx_size invalid")
    if not 10 <= int(q["supervisor_poll_seconds"]) <= 3600:
        raise ValueError("qwen.supervisor_poll_seconds must be 10..3600")
    ncfg = cfg["runtime"]
    if type(ncfg["context_enabled"]) is not bool or type(ncfg["context_persistence_enabled"]) is not bool:
        raise ValueError("context toggles must be booleans")
    if ncfg["context_persistence_enabled"] and not ncfg["context_enabled"]:
        raise ValueError("context_persistence_enabled requires context_enabled")
    if (type(ncfg["context_owner"]) is not str
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,31}",ncfg["context_owner"])):
        raise ValueError("Invalid context owner")
    if ncfg["context_mode"] not in ("assistant","meeting"):
        raise ValueError("Invalid context mode")
    if type(ncfg["context_ttl_seconds"]) is not int or not 1 <= ncfg["context_ttl_seconds"] <= 300:
        raise ValueError("context_ttl_seconds must be 1..300")
    if type(ncfg["context_budget_chars"]) is not int or not 256 <= ncfg["context_budget_chars"] <= 3200:
        raise ValueError("context_budget_chars must be 256..3200")
    if type(ncfg["needle_loop_enabled"]) is not bool:
        raise ValueError("needle_loop_enabled must be a boolean")
    if type(ncfg["needle_max_decisions"]) is not int or not 1 <= ncfg["needle_max_decisions"] <= 3:
        raise ValueError("needle_max_decisions must be 1..3")
    if type(ncfg["needle_max_tools"]) is not int or not 1 <= ncfg["needle_max_tools"] <= 3:
        raise ValueError("needle_max_tools must be 1..3")
    if type(ncfg["needle_context_ttl_seconds"]) is not int or not 1 <= ncfg["needle_context_ttl_seconds"] <= 300:
        raise ValueError("needle_context_ttl_seconds must be 1..300")
    if type(cfg["runtime"]["harness_enabled"]) is not bool:
        raise ValueError("harness_enabled must be a boolean")
    if type(cfg["runtime"]["harness_timeout_seconds"]) is not int or not 5 <= cfg["runtime"]["harness_timeout_seconds"] <= 120:
        raise ValueError("harness_timeout_seconds must be 5..120")
    if type(cfg["runtime"]["cognitive_diagnostics"]) is not bool:
        raise ValueError("cognitive_diagnostics must be a boolean")
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
