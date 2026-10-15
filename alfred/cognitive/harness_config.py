"""Pinned SDK manifest constants for the audited DeepSeek Harness release.

These values represent the exact, audited state of the sdk-minimal Cordis
plugin graph for SDK_VERSION. Any deviation causes LocalHarness to fail
closed before accepting a model request.
"""
from __future__ import annotations

SDK_VERSION = "0.1.5rc1"

# Exact shipped sdk-minimal row IDs from the audited release family. The
# overlay deliberately disables all model-facing tools and host side effects.
DENIED_ROWS: frozenset[str] = frozenset({
    "persistent-bash", "persistent-pwsh", "terminal-bash", "terminal-pwsh",
    "pty", "subprocess", "sandbox-policy", "sandbox", "filesystem",
    "tool-fs", "attachment-local", "working-directory",
    "tool-working-directory", "mcp-resources", "jobs",
    "plugin-package-inventory-deepseek", "session-log-deepseek",
    "compaction-basic", "image-offload",
})

# Exact effective `dsh --profile sdk-minimal --patch ... --dump-config`
# inventory captured from the official pinned 0.1.5rc1 binary. All registered
# plugins, including invariant providers, must match; no additional plugins
# are permitted. The model-facing tools registry must have ZERO actual tools.
EXPECTED_ACTIVE_ROWS: dict[str, str] = {
    "sdk-app-startup": "@deepseek-ai/dsh-sdk-app",
    "sdk-jsonrpc-server": "@deepseek-ai/dsh-sdk-jsonrpc-server",
    "deepseek-llm-api-extensions": "@deepseek-ai/dsh-deepseek-llm-api-extensions",
    "llm-deepseek": "@deepseek-ai/dsh-llm-deepseek",
    "session-projection": "@deepseek-ai/dsh-session-projection",
    "timer": "@deepseek-ai/cordis-plugin-timer",
    "llm": "@deepseek-ai/dsh-llm",
    "session": "@deepseek-ai/dsh-session",
    "session-title": "@deepseek-ai/dsh-session-title",
    "system-prompt": "@deepseek-ai/dsh-system-prompt",
    "tools": "@deepseek-ai/dsh-tools",
    "agent": "@deepseek-ai/dsh-agent",
    "llm-retry": "@deepseek-ai/dsh-llm-retry",
    "invariants": "@deepseek-ai/dsh-invariants",
    "session-invariant": "@deepseek-ai/dsh-session/invariant",
    "agent-invariant": "@deepseek-ai/dsh-agent/invariant",
    "scope-invariant": "@deepseek-ai/dsh-scope/invariant",
    "agent-loop-invariant": "@deepseek-ai/dsh-agent-loop/invariant",
    "agent-loop": "@deepseek-ai/dsh-agent-loop",
    "sessions": "@deepseek-ai/dsh-session-persistence-jsonl",
}
EXPECTED_DISABLED_ROWS: dict[str, str] = {
    "session-log-deepseek": "@deepseek-ai/dsh-session-log-deepseek",
    "plugin-package-inventory-deepseek": "@deepseek-ai/dsh-plugin-package-inventory-deepseek",
    "sandbox": "@deepseek-ai/dsh-sandbox-local",
    "sandbox-policy": "@deepseek-ai/dsh-sandbox-policy",
    "subprocess": "@deepseek-ai/dsh-subprocess-local",
    "pty": "@deepseek-ai/dsh-terminal",
    "terminal-bash": "@deepseek-ai/dsh-terminal-bash",
    "terminal-pwsh": "@deepseek-ai/dsh-terminal-bash",
    "jobs": "@deepseek-ai/dsh-jobs-local",
    "persistent-bash": "@deepseek-ai/dsh-tool-bash-persistent",
    "persistent-pwsh": "@deepseek-ai/dsh-tool-pwsh-persistent",
}
