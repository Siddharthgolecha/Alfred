"""Validated, local configuration; no secrets committed to source control."""
import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .constants import Limits, Patterns, Timeouts, TTL
from .validators import validate_bool, validate_int, validate_ttl_seconds

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
        "harness_timeout_seconds": 45,
        "needle_loop_enabled": True, "escalation_enabled": True, "context_enabled": True,
        "resource_admission_enabled": True,
        "resource_min_available_mib": 512,
        "resource_max_swap_used_mib": 1024,
        "resource_max_cpu_load_per_core": 3.0,
        "resource_max_wait_ms": 200,
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
            or not Patterns.WAKE_PHRASE.fullmatch(wake["phrase"])
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
    from .inference_admission import InferenceAdmission
    InferenceAdmission(
        enabled=ncfg["resource_admission_enabled"],
        min_available_mib=ncfg["resource_min_available_mib"],
        max_swap_used_mib=ncfg["resource_max_swap_used_mib"],
        max_cpu_load_per_core=ncfg["resource_max_cpu_load_per_core"],
        max_wait_ms=ncfg["resource_max_wait_ms"])
    validate_bool(ncfg["escalation_enabled"], "escalation_enabled")
    validate_bool(ncfg["context_enabled"], "context_enabled")
    validate_bool(ncfg["context_persistence_enabled"], "context_persistence_enabled")
    if ncfg["context_persistence_enabled"] and not ncfg["context_enabled"]:
        raise ValueError("context_persistence_enabled requires context_enabled")
    if (type(ncfg["context_owner"]) is not str
            or not Patterns.CONTEXT_OWNER.fullmatch(ncfg["context_owner"])):
        raise ValueError("Invalid context owner")
    if ncfg["context_mode"] not in ("assistant","meeting"):
        raise ValueError("Invalid context mode")
    validate_ttl_seconds(ncfg["context_ttl_seconds"], min_val=TTL.CONTEXT_MIN, max_val=TTL.CONTEXT_MAX, label="context_ttl_seconds")
    validate_int(ncfg["context_budget_chars"], "context_budget_chars")
    if not Limits.CONTEXT_BUDGET_CHARS_MIN <= ncfg["context_budget_chars"] <= Limits.CONTEXT_BUDGET_CHARS_MAX:
        raise ValueError(f"context_budget_chars must be {Limits.CONTEXT_BUDGET_CHARS_MIN}..{Limits.CONTEXT_BUDGET_CHARS_MAX}")
    validate_bool(ncfg["needle_loop_enabled"], "needle_loop_enabled")
    validate_int(ncfg["needle_max_decisions"], "needle_max_decisions")
    if not 1 <= ncfg["needle_max_decisions"] <= Limits.NEEDLE_MAX_DECISIONS:
        raise ValueError(f"needle_max_decisions must be 1..{Limits.NEEDLE_MAX_DECISIONS}")
    validate_int(ncfg["needle_max_tools"], "needle_max_tools")
    if not 1 <= ncfg["needle_max_tools"] <= Limits.NEEDLE_MAX_TOOLS:
        raise ValueError(f"needle_max_tools must be 1..{Limits.NEEDLE_MAX_TOOLS}")
    validate_ttl_seconds(ncfg["needle_context_ttl_seconds"], min_val=TTL.NEEDLE_SLOT_MIN, max_val=TTL.NEEDLE_SLOT_MAX, label="needle_context_ttl_seconds")
    validate_int(cfg["runtime"]["harness_timeout_seconds"], "harness_timeout_seconds")
    if not Timeouts.HARNESS_TIMEOUT_MIN <= cfg["runtime"]["harness_timeout_seconds"] <= Timeouts.HARNESS_TIMEOUT_MAX:
        raise ValueError(f"harness_timeout_seconds must be {Timeouts.HARNESS_TIMEOUT_MIN}..{Timeouts.HARNESS_TIMEOUT_MAX}")
    validate_bool(cfg["runtime"]["cognitive_diagnostics"], "cognitive_diagnostics")
    if not 0.0 < float(cfg["runtime"]["needle_confidence_floor"]) <= 1.0:
        raise ValueError("needle_confidence_floor must be in (0,1]")
    if not TTL.APPROVAL_MIN <= int(cfg["runtime"]["approval_ttl_seconds"]) <= TTL.APPROVAL_MAX:
        raise ValueError(f"approval_ttl_seconds must be {TTL.APPROVAL_MIN}..{TTL.APPROVAL_MAX}")
    if not 1 <= int(cfg["runtime"]["max_plan_steps"]) <= Limits.MAX_PLAN_STEPS * 2:
        raise ValueError(f"max_plan_steps must be 1..{Limits.MAX_PLAN_STEPS * 2}")
    return cfg

def data_path(value):
    path = Path(value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
