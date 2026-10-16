"""Validated, local configuration; no secrets committed to source control."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .constants import Limits, Patterns, Timeouts, TTL

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

def _merge(base: dict, update: dict) -> None:
    for key, value in update.items():
        if key not in base:
            raise ValueError(f"Unknown configuration key: {key}")
        if key == "named_tasks" and isinstance(value, dict):
            base[key] = value
        elif isinstance(base[key], dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _load_raw(path: str | Path | None) -> dict:
    cfg = copy.deepcopy(DEFAULT)
    if path:
        with open(path, encoding="utf-8") as fh:
            _merge(cfg, json.load(fh))
    return cfg


def _merge_defaults(cfg: dict) -> dict:
    return cfg


def _apply_env_overrides(cfg: dict, path: str | Path | None) -> dict:
    from .backends import backend_kind, DEFAULT_PORTS

    qwen_cfg = cfg["qwen"]
    qwen_cfg["model_path"] = os.environ.get("ALFRED_GGUF", qwen_cfg["model_path"])
    qwen_cfg["binary"] = os.environ.get(
        "ALFRED_QWEN_BINARY",
        os.environ.get("ALFRED_LLAMA_SERVER", qwen_cfg["binary"]),
    )
    qwen_cfg["model"] = os.environ.get("ALFRED_QWEN_MODEL", qwen_cfg["model"])
    qwen_cfg["backend"] = (
        os.environ.get("ALFRED_QWEN_BACKEND", qwen_cfg["backend"])
        .lower().replace("-", "_").replace(".", "_")
    )
    qwen_cfg["backend"] = {
        "lmstudio": "lm_studio", "lms": "lm_studio"
    }.get(qwen_cfg["backend"], qwen_cfg["backend"])

    selected = backend_kind(qwen_cfg)
    explicit_url = (
        bool(
            path
            and json.loads(Path(path).read_text(encoding="utf-8"))
               .get("qwen", {}).get("url")
        )
        or "ALFRED_QWEN_URL" in os.environ
        or "ALFRED_LLAMA_URL" in os.environ
    )
    default_url = f"http://127.0.0.1:{DEFAULT_PORTS[selected]}"
    qwen_cfg["url"] = os.environ.get(
        "ALFRED_QWEN_URL",
        os.environ.get(
            "ALFRED_LLAMA_URL",
            qwen_cfg["url"] if explicit_url else default_url,
        ),
    ).rstrip("/")

    return cfg


def _validate(cfg: dict) -> None:
    qwen_cfg = cfg["qwen"]
    if qwen_cfg["backend"] not in ("auto", "llama_cpp", "llama_server", "ollama", "lm_studio"):
        raise ValueError("qwen.backend must be auto/llama_cpp/llama_server/ollama/lm_studio")
    parts = urlsplit(qwen_cfg["url"])
    if (parts.scheme != "http"
            or parts.hostname not in ("127.0.0.1", "localhost", "::1")
            or parts.username or parts.password or parts.port is None):
        raise ValueError("qwen.url must be a loopback HTTP origin with an explicit port")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("qwen.url must be the server origin (without /v1)")
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


def load_config(path=None):
    cfg = _load_raw(path)
    cfg = _merge_defaults(cfg)
    cfg = _apply_env_overrides(cfg, path)
    _validate(cfg)
    return cfg

def data_path(value: str | Path) -> Path:
    """Expand and ensure parent directory exists, then return the resolved path."""
    path = Path(value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Pydantic v2 typed configuration models
# ---------------------------------------------------------------------------

class AudioConfig(BaseModel):
    """Validated audio capture settings."""

    model_config = {"extra": "forbid"}

    mode: str = "auto"
    sony_name: str = "WI-C100"
    bluetooth_address: str = ""
    direct_source: str = ""
    poll_seconds: int = 5
    frame_ms: int = 500
    speech_rms: float = 0.013
    end_silence_frames: int = 2
    max_utterance_seconds: int = 25
    auto_fallback: bool = True
    probe_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 5.0

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in ("auto", "direct", "bluetooth"):
            raise ValueError("audio.mode must be auto/direct/bluetooth")
        return v

    @field_validator("frame_ms")
    @classmethod
    def _validate_frame_ms(cls, v: int) -> int:
        if v not in (250, 500, 1000):
            raise ValueError("audio.frame_ms must be 250, 500 or 1000")
        return v

    @field_validator("probe_timeout_seconds", "read_timeout_seconds")
    @classmethod
    def _validate_timeouts(cls, v: float) -> float:
        if not Limits.AUDIO_TIMEOUT_MIN <= v <= Limits.AUDIO_TIMEOUT_MAX:
            raise ValueError(
                f"audio timeout must be "
                f"{Limits.AUDIO_TIMEOUT_MIN}..{Limits.AUDIO_TIMEOUT_MAX}"
            )
        return v

    @field_validator("speech_rms")
    @classmethod
    def _validate_rms(cls, v: float) -> float:
        if not Limits.AUDIO_SPEECH_RMS_MIN <= v <= Limits.AUDIO_SPEECH_RMS_MAX:
            raise ValueError("audio.speech_rms out of range")
        return v


class WakeWordConfig(BaseModel):
    """Validated wake-word detector settings."""

    model_config = {"extra": "forbid"}

    enabled: bool = True
    phrase: str = "Alfred"
    mode: str = "transcript"
    followup_seconds: float = 8.0
    model_path: str = ""
    threshold: float = 0.75

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in ("transcript", "openwakeword"):
            raise ValueError("wake_word.mode must be transcript or openwakeword")
        return v

    @field_validator("phrase")
    @classmethod
    def _validate_phrase(cls, v: str) -> str:
        if not isinstance(v, str) or not Patterns.WAKE_PHRASE.fullmatch(v) or v != v.strip():
            raise ValueError("wake_word.phrase must be a nonempty literal word/phrase")
        return v

    @field_validator("followup_seconds")
    @classmethod
    def _validate_followup(cls, v: float) -> float:
        if not 0 <= v <= Limits.WAKE_FOLLOWUP_MAX:
            raise ValueError(f"wake_word.followup_seconds must be 0..{Limits.WAKE_FOLLOWUP_MAX}")
        return v

    @field_validator("threshold")
    @classmethod
    def _validate_threshold(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("wake_word.threshold must be in (0,1]")
        return v


class QwenConfig(BaseModel):
    """Validated Qwen / llama backend settings."""

    model_config = {"extra": "forbid"}

    backend: str = "auto"
    url: str = ""
    binary: str = ""
    model: str = ""
    model_path: str = ""
    expected_model: str = "Qwen3.5-4B"
    ctx_size: int = 4096
    gpu_layers: int = 99
    startup_timeout_seconds: int = 120
    max_tokens: int = 500
    enabled: bool = True
    supervisor_poll_seconds: int = 30

    @field_validator("ctx_size")
    @classmethod
    def _validate_ctx(cls, v: int) -> int:
        if not Limits.QWEN_CTX_SIZE_MIN <= v <= Limits.QWEN_CTX_SIZE_MAX:
            raise ValueError(
                f"qwen.ctx_size must be "
                f"{Limits.QWEN_CTX_SIZE_MIN}..{Limits.QWEN_CTX_SIZE_MAX}"
            )
        return v

    @field_validator("supervisor_poll_seconds")
    @classmethod
    def _validate_poll(cls, v: int) -> int:
        if not Limits.QWEN_POLL_MIN <= v <= Limits.QWEN_POLL_MAX:
            raise ValueError(
                f"qwen.supervisor_poll_seconds must be "
                f"{Limits.QWEN_POLL_MIN}..{Limits.QWEN_POLL_MAX}"
            )
        return v


class RuntimeConfig(BaseModel):
    """Validated runtime / policy settings."""

    model_config = {"extra": "forbid"}

    database: str = "~/.local/share/alfred/alfred.db"
    logs: str = "~/.local/state/alfred"
    allowed_services: list[str] = Field(default_factory=list)
    named_tasks: dict[str, Any] = Field(default_factory=dict)
    needle_confidence_floor: float = 0.7
    max_plan_steps: int = Limits.MAX_PLAN_STEPS
    approval_ttl_seconds: int = TTL.APPROVAL_DEFAULT
    tts: bool = False
    voice_alerts: bool = False
    telemetry: bool = False
    cognitive_diagnostics: bool = False
    harness_timeout_seconds: int = 45
    needle_loop_enabled: bool = True
    escalation_enabled: bool = True
    context_enabled: bool = True
    resource_admission_enabled: bool = True
    resource_min_available_mib: int = 512
    resource_max_swap_used_mib: int = 1024
    resource_max_cpu_load_per_core: float = 3.0
    resource_max_wait_ms: int = 200
    context_persistence_enabled: bool = False
    context_owner: str = "local"
    context_mode: str = "assistant"
    context_ttl_seconds: int = TTL.CONTEXT_DEFAULT
    context_budget_chars: int = 1200
    needle_max_decisions: int = Limits.NEEDLE_MAX_DECISIONS
    needle_max_tools: int = Limits.NEEDLE_MAX_TOOLS
    needle_context_ttl_seconds: int = TTL.NEEDLE_SLOT_DEFAULT

    @field_validator("context_ttl_seconds")
    @classmethod
    def _validate_ctx_ttl(cls, v: int) -> int:
        if not TTL.CONTEXT_MIN <= v <= TTL.CONTEXT_MAX:
            raise ValueError(f"context_ttl_seconds must be {TTL.CONTEXT_MIN}..{TTL.CONTEXT_MAX}")
        return v

    @field_validator("needle_context_ttl_seconds")
    @classmethod
    def _validate_needle_ttl(cls, v: int) -> int:
        if not TTL.NEEDLE_SLOT_MIN <= v <= TTL.NEEDLE_SLOT_MAX:
            raise ValueError(f"needle_context_ttl_seconds must be {TTL.NEEDLE_SLOT_MIN}..{TTL.NEEDLE_SLOT_MAX}")
        return v

    @field_validator("approval_ttl_seconds")
    @classmethod
    def _validate_approval_ttl(cls, v: int) -> int:
        if not TTL.APPROVAL_MIN <= v <= TTL.APPROVAL_MAX:
            raise ValueError(f"approval_ttl_seconds must be {TTL.APPROVAL_MIN}..{TTL.APPROVAL_MAX}")
        return v

    @field_validator("harness_timeout_seconds")
    @classmethod
    def _validate_harness_timeout(cls, v: int) -> int:
        if not Timeouts.HARNESS_TIMEOUT_MIN <= v <= Timeouts.HARNESS_TIMEOUT_MAX:
            raise ValueError(
                f"harness_timeout_seconds must be "
                f"{Timeouts.HARNESS_TIMEOUT_MIN}..{Timeouts.HARNESS_TIMEOUT_MAX}"
            )
        return v

    @field_validator("max_plan_steps")
    @classmethod
    def _validate_max_steps(cls, v: int) -> int:
        if not 1 <= v <= Limits.MAX_PLAN_STEPS * 2:
            raise ValueError(f"max_plan_steps must be 1..{Limits.MAX_PLAN_STEPS * 2}")
        return v

    @field_validator("needle_max_decisions")
    @classmethod
    def _validate_max_decisions(cls, v: int) -> int:
        if not 1 <= v <= Limits.NEEDLE_MAX_DECISIONS:
            raise ValueError(f"needle_max_decisions must be 1..{Limits.NEEDLE_MAX_DECISIONS}")
        return v

    @field_validator("needle_max_tools")
    @classmethod
    def _validate_max_tools(cls, v: int) -> int:
        if not 1 <= v <= Limits.NEEDLE_MAX_TOOLS:
            raise ValueError(f"needle_max_tools must be 1..{Limits.NEEDLE_MAX_TOOLS}")
        return v

    @field_validator("context_budget_chars")
    @classmethod
    def _validate_budget_chars(cls, v: int) -> int:
        if not Limits.CONTEXT_BUDGET_CHARS_MIN <= v <= Limits.CONTEXT_BUDGET_CHARS_MAX:
            raise ValueError(
                f"context_budget_chars must be "
                f"{Limits.CONTEXT_BUDGET_CHARS_MIN}..{Limits.CONTEXT_BUDGET_CHARS_MAX}"
            )
        return v

    @field_validator("context_owner")
    @classmethod
    def _validate_context_owner(cls, v: str) -> str:
        if not Patterns.CONTEXT_OWNER.fullmatch(v):
            raise ValueError("Invalid context owner")
        return v

    @field_validator("context_mode")
    @classmethod
    def _validate_context_mode(cls, v: str) -> str:
        if v not in ("assistant", "meeting"):
            raise ValueError("Invalid context mode")
        return v

    @model_validator(mode="after")
    def _validate_context_persistence(self) -> "RuntimeConfig":
        if self.context_persistence_enabled and not self.context_enabled:
            raise ValueError("context_persistence_enabled requires context_enabled")
        return self


class AlfredConfig(BaseModel):
    """Top-level typed configuration for Alfred.

    Construct via :func:`load_config_model` rather than directly.
    """

    audio: AudioConfig = Field(default_factory=AudioConfig)
    wake_word: WakeWordConfig = Field(default_factory=WakeWordConfig)
    qwen: QwenConfig = Field(default_factory=QwenConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


def load_config_model(path: str | Path | None = None) -> AlfredConfig:
    """Load, validate, and return a fully typed :class:`AlfredConfig`.

    Applies the same env-var overrides as :func:`load_config` before building
    the Pydantic model.  This function is the preferred entry point for new
    code; existing code may still use :func:`load_config` which returns the
    legacy ``dict``.
    """
    raw = load_config(path)
    return AlfredConfig(
        audio=AudioConfig(**raw["audio"]),
        wake_word=WakeWordConfig(**raw["wake_word"]),
        qwen=QwenConfig(**raw["qwen"]),
        runtime=RuntimeConfig(**raw["runtime"]),
    )
