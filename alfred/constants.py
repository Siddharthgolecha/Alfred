"""Centralised named constants for Alfred.

All magic numbers, string limits, timeouts, TTL ranges, and compiled regex
patterns live here. Import from this module instead of repeating raw literals.
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# String truncation lengths (characters or bytes, as documented per field)
# ---------------------------------------------------------------------------
class Truncation:
    """Maximum lengths used when slicing strings before storage or display."""

    # Short single-line user/model messages forwarded to logs or UI.
    MESSAGE: int = 300
    # Longer model/tool output retained in the task ledger or event log.
    RESULT: int = 1200
    # Stderr captured from subprocess tools.
    STDERR: int = 600
    # Raw tegrastats line stored in gpu_status output.
    GPU_RAW: int = 700
    # Full tool-result JSON passed to the explain endpoint.
    EXPLAIN_OBSERVATIONS: int = 7000
    # User question passed to the explain endpoint.
    EXPLAIN_QUESTION: int = 1200
    # Qwen explanation answer returned to the caller.
    EXPLAIN_ANSWER: int = 1400
    # Event-log detail column (SQLite TEXT).
    EVENT_DETAIL: int = 6000
    # Approval error string stored in result_json.
    APPROVAL_ERROR: int = 300
    # Approval result_json column (SQLite TEXT).
    APPROVAL_RESULT_JSON: int = 10000
    # Maximum bytes for a single cognitive contract (UTF-8 encoded).
    CONTRACT_BYTES: int = 2048
    # Cordis --dump-config output accepted for audit.
    CORDIS_DUMP_BYTES: int = 200000
    # Cordis overlay patch file.
    PATCH_BYTES: int = 4096


# ---------------------------------------------------------------------------
# Numeric limits (counts, ranges, capacities)
# ---------------------------------------------------------------------------
class Limits:
    """Counts and capacity caps used throughout the system."""

    # Maximum services returned by list_services (context safety cap).
    MAX_SERVICES: int = 32
    # Maximum rows returned by list_reminders.
    MAX_REMINDER_LIST: int = 100
    # Maximum Needle decisions per loop iteration.
    NEEDLE_MAX_DECISIONS: int = 3
    # Maximum Needle tool calls per loop iteration.
    NEEDLE_MAX_TOOLS: int = 3
    # Maximum plan steps (default; overridable via config).
    MAX_PLAN_STEPS: int = 5
    # Maximum task observations stored per task in the context tier.
    MAX_TASK_OBSERVATIONS: int = 3
    # Maximum retained facts per owner+mode scope.
    MAX_FACTS: int = 8
    # Maximum diagnostic metric entries per Diagnostic contract.
    MAX_DIAGNOSTIC_METRICS: int = 5
    # Maximum LocalHarness SDK response characters.
    HARNESS_MAX_RESPONSE: int = 2000
    # Maximum LocalHarness planning prompt characters.
    HARNESS_MAX_PROMPT: int = 8192
    # Maximum planning utterance characters.
    HARNESS_MAX_UTTERANCE: int = 1600
    # Tool string argument maximum length (schema-level).
    TOOL_STR_MAX_LENGTH: int = 240
    # Maximum service/task name length.
    VALID_NAME_MAX_LENGTH: int = 80
    # Maximum tool name length in plans.
    PLAN_TOOL_NAME_MAX: int = 128
    # Maximum Qwen message field length in plan response.
    QWEN_MESSAGE_MAX: int = 800
    # Maximum Qwen explanation answer length.
    QWEN_ANSWER_MAX: int = 1400
    # Maximum Qwen explain observation JSON characters.
    QWEN_EXPLAIN_OBS_MAX: int = 7000
    # Maximum meeting segments per recording.
    MAX_MEETING_SEGMENTS: int = 64
    # Maximum meeting evidence bytes.
    MAX_EVIDENCE_BYTES: int = 16000
    # Maximum timestamp string length.
    MAX_TIMESTAMP_LEN: int = 40
    # Maximum approval integer ID (SQLite INTEGER range).
    MAX_APPROVAL_ID: int = 2147483647
    # Maximum Diagnostic latency_ms value.
    DIAG_MAX_LATENCY_MS: int = 300000
    # Maximum Diagnostic input/output_chars value.
    DIAG_MAX_CHARS: int = 100000
    # Maximum Diagnostic retry_count value.
    DIAG_MAX_RETRY: int = 100
    # Maximum Diagnostic channels value.
    DIAG_MAX_CHANNELS: int = 32
    # Maximum tool call count recorded in a Decision contract.
    DECISION_MAX_CALL_COUNT: int = 50
    # Maximum task input characters recorded in a TaskEnvelope contract.
    TASK_MAX_INPUT_CHARS: int = 100000
    # Maximum scalar telemetry value accepted by _bounded_metrics.
    METRICS_MAX_VALUE: float = 1e9
    # Maximum GPU temperature (°C) considered valid for a referent slot.
    GPU_TEMP_MAX_C: float = 150.0
    # Minimum GPU temperature (°C) considered valid for a referent slot.
    GPU_TEMP_MIN_C: float = -50.0
    # Maximum Qwen plan steps (schema-level).
    QWEN_PLAN_MAX_STEPS: int = 5
    # Maximum retained context budget (chars).
    CONTEXT_BUDGET_CHARS_MAX: int = 3200
    # Minimum retained context budget (chars).
    CONTEXT_BUDGET_CHARS_MIN: int = 256
    # Maximum harness model identifier length.
    MODEL_ID_MAX_LENGTH: int = 120
    # Maximum LocalHarness max_tokens.
    HARNESS_MAX_TOKENS: int = 512
    # Qwen context-window size range.
    QWEN_CTX_SIZE_MIN: int = 1
    QWEN_CTX_SIZE_MAX: int = 131072
    # Qwen supervisor poll interval range (seconds).
    QWEN_POLL_MIN: int = 10
    QWEN_POLL_MAX: int = 3600
    # Audio stream timeout range (seconds).
    AUDIO_TIMEOUT_MIN: int = 1
    AUDIO_TIMEOUT_MAX: int = 30
    # Audio speech RMS energy range.
    AUDIO_SPEECH_RMS_MIN: float = 0.001
    AUDIO_SPEECH_RMS_MAX: float = 0.2
    # Wake-word followup window upper bound (seconds).
    WAKE_FOLLOWUP_MAX: int = 30


# ---------------------------------------------------------------------------
# Timeout and duration values (seconds unless stated)
# ---------------------------------------------------------------------------
class Timeouts:
    """Timeout durations used for network calls, subprocesses, and threads."""

    # Default HTTP JSON request timeout (models.request_json).
    REQUEST_JSON: int = 4
    # Qwen chat completion request timeout.
    QWEN_COMPLETION: int = 90
    # Subprocess run_named_task timeout.
    NAMED_TASK: int = 60
    # systemctl service control timeout.
    SERVICE_CONTROL: int = 10
    # Scheduler thread join on stop.
    SCHEDULER_STOP: int = 4
    # tegrastats select() read timeout.
    TEGRASTATS_READ: int = 4
    # tegrastats process terminate wait.
    TEGRASTATS_TERMINATE: int = 2
    # Cordis --dump-config audit subprocess timeout.
    CORDIS_AUDIT: int = 15
    # LocalHarness minimum allowed timeout (harness_timeout_seconds lower bound).
    HARNESS_TIMEOUT_MIN: int = 5
    # LocalHarness maximum allowed timeout.
    HARNESS_TIMEOUT_MAX: int = 120
    # LocalHarness SDK initialize_timeout cap.
    HARNESS_INIT_CAP: int = 30
    # LocalHarness SDK shutdown timeout.
    HARNESS_SHUTDOWN: float = 1.0


# ---------------------------------------------------------------------------
# TTL and range bounds (seconds)
# ---------------------------------------------------------------------------
class TTL:
    """Time-to-live defaults and valid ranges (all in seconds)."""

    # Default approval TTL.
    APPROVAL_DEFAULT: int = 180
    # Minimum approval TTL.
    APPROVAL_MIN: int = 30
    # Maximum approval TTL.
    APPROVAL_MAX: int = 3600
    # Default context slot TTL.
    CONTEXT_DEFAULT: int = 120
    # Minimum context TTL.
    CONTEXT_MIN: int = 1
    # Maximum context TTL.
    CONTEXT_MAX: int = 300
    # Default Needle session slot TTL.
    NEEDLE_SLOT_DEFAULT: int = 120
    # Minimum needle_context_ttl_seconds.
    NEEDLE_SLOT_MIN: int = 1
    # Maximum needle_context_ttl_seconds.
    NEEDLE_SLOT_MAX: int = 300


# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------
class Patterns:
    """Centralised compiled regex patterns shared across modules."""

    # 32-character lowercase hex UUID used as correlation IDs.
    CORRELATION_ID: re.Pattern[str] = re.compile(r"^[0-9a-f]{32}$")
    # Cognitive contract tool name.
    TOOL_NAME: re.Pattern[str] = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    # Context scope / owner identifier.
    SCOPE_OWNER: re.Pattern[str] = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
    # Context fact key.
    FACT_KEY: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
    # Service/task name used in tool allowlists.
    VALID_NAME: re.Pattern[str] = re.compile(r"[a-zA-Z0-9_.@-]{1,80}")
    # Plan condition field (safe Python identifier-like).
    CONDITION_FIELD: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    # LocalHarness / Qwen model identifier.
    MODEL_ID: re.Pattern[str] = re.compile(r"[a-zA-Z0-9_.:/-]{1,120}")
    # Wake-word phrase.
    WAKE_PHRASE: re.Pattern[str] = re.compile(r"[A-Za-z][A-Za-z0-9 ]{0,39}")
    # Workflow / save_as name.
    WORKFLOW_NAME: re.Pattern[str] = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{1,40}")
    # Context owner identifier (alias of SCOPE_OWNER for clarity).
    CONTEXT_OWNER: re.Pattern[str] = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,31}")
    # Cordis plugin row ID.
    CORDIS_ROW_ID: re.Pattern[str] = re.compile(r"[a-z][a-z0-9-]*")
    # Cordis overlay patch line prefix.
    CORDIS_PATCH_LINE: re.Pattern[str] = re.compile(r"- id: [a-z][a-z0-9-]*")
    # GPU temperature mention in user text.
    GPU_TEMP: re.Pattern[str] = re.compile(
        r"\b(?:gpu|graphics\s+(?:card|processor))\b.*\btemp(?:erature)?\b|"
        r"\btemp(?:erature)?\b.*\b(?:gpu|graphics\s+(?:card|processor))\b",
        re.I,
    )
    # Ambiguous pronoun reference (safe read-only).
    PRONOUN_REF: re.Pattern[str] = re.compile(
        r"^\s*(?:check|read|show|get)\s+(?:it|that)(?:\s+again)?[.!?]*\s*$",
        re.I,
    )
    # Dangerous pronoun reference (action verbs).
    DANGEROUS_REF: re.Pattern[str] = re.compile(
        r"^\s*(?:restart|stop|start|cancel|delete|run|"
        r"approve|change|disable|enable)\s+(?:it|that)(?:\s+again)?[.!?]*\s*$",
        re.I,
    )
    # Explicit system-status mention.
    EXPLICIT_SYSTEM: re.Pattern[str] = re.compile(
        r"\b(?:system|machine|computer)\s+(?:status|health)\b", re.I
    )
    # Explicit workflow mention.
    EXPLICIT_WORKFLOW: re.Pattern[str] = re.compile(r"\bworkflow\b", re.I)
    # Multi-step chaining keywords.
    MULTISTEP: re.Pattern[str] = re.compile(
        r"\b(?:then|afterwards|after that|next|depending on|based on)\b", re.I
    )
    # Explanation-intent keywords.
    WANTS_EXPLANATION: re.Pattern[str] = re.compile(
        r"\b(why|explain|how|compare|interpret|mean|meaning)\b", re.I
    )
    # Forbidden context fact key words.
    FACT_KEY_FORBIDDEN: re.Pattern[str] = re.compile(
        r"secret|password|token|api_key|command|script|system|"
        r"prompt|instruction|shell|tool|policy|approval|credential",
        re.I,
    )
    # Sensitive value patterns (API keys, bearer tokens, injection attempts).
    SENSITIVE_VALUE: re.Pattern[str] = re.compile(
        r"(?i)(?:sk-[A-Za-z0-9]{8,}|bearer\s+\S+|"
        r"ignore\s+(?:previous|all)\s+instructions|"
        r"(?:^|\s)(?:sudo|curl|wget)\s|rm\s+-rf|https?://)"
    )
    # Meeting segment / evidence secret patterns.
    MEETING_SECRET: re.Pattern[str] = re.compile(
        r"(?i)(?:sk-[A-Za-z0-9]{8,}|bearer\s+[^\s]+|"
        r"(?<![\w.-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    )
    # Meeting control characters.
    MEETING_CONTROLS: re.Pattern[str] = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
    # Meeting sensitive commands.
    MEETING_SENSITIVE_CMD: re.Pattern[str] = re.compile(
        r"(?i)\b(?:run|install|execute|approve|ignore)\b"
    )
    # Meeting ID.
    MEETING_ID: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
    # Meeting source.
    MEETING_SOURCE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
    # Meeting artifact.
    MEETING_ARTIFACT: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{8,72}$")
    # Meeting owner.
    MEETING_OWNER: re.Pattern[str] = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    # Generic meeting entity ID.
    MEETING_ENTITY_ID: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
