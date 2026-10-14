# DeepSeek Harness integration: security-gated prototype

## Current implementation

The optional Python event bridge `alfred/cognitive/harness_bridge.py` is disabled by default. It validates versioned, size-limited structural events and can exchange them with an injected fake transport. It **does not launch or claim compatibility with the real SDK**, modify Alfred's model routing, grant tool execution, or change System 2 approvals. The original Needle/Qwen architecture remains operational and unchanged.

The accepted envelope is `{"version":1,"event":"observation","payload":{"tool":"status","outcome":"returned"}}`. It is deliberately *not* the official Cordis JSON-RPC protocol. All execution proposals, remote endpoints, plugin injection, raw prompts, transcripts, tool arguments and results are rejected by the bridge.

## Why not launch sdk-minimal?

Official DeepSeek Harness documentation describes the `sdk-minimal` bundle as enabling a persistent shell with broad local filesystem privileges. A model prompt cannot safely disable those privileges. Consequently Alfred **must not** launch the stock profile, even in a subprocess, without a separately audited Cordis composition that removes executable tools and restricts the provider. The SDK also stores JSONL session logs, which can violate Alfred's default transcript privacy contract. See https://github.com/deepseek-ai/deepseek-harness/tree/master/python/sdk and https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/bundle/sdk-minimal (these are research links, not dependency pins).

## Required before activation

1. Resolve a published exact `deepseek-harness-sdk` version and its matching runtime wheel, verify SHA-256 for x86_64 and Linux ARM64, and commit the distribution manifest. Do not use floating `master`, `main`, or an unverified third-party wheel.
2. Compose and inspect a dedicated Cordis profile with no shell, editor, unrestricted filesystem, web, network connector, plugin manager or persistent transcript logging. Explicitly verify the dumped plugin tree and deny-list tools. Do not rely on `sdk-minimal` defaults.
3. Add a genuine SDK adapter that connects only to the preexisting local Qwen OpenAI-compatible endpoint, without launching another model server; keep Needle native behind the Python adapter. Define stable task/session mapping and do not confuse this bridge's structural events with SDK wire frames.
4. Route any model-suggested tool call through Alfred's current `System2` preflight and protected approvals. No harness-issued approvals or replayed tool calls. Add bounded cancellation, graceful process shutdown and crash fallback.
5. Run a real SDK smoke test with a stub loopback model, negative tests for unsafe shell, remote URLs, tool spoofing, secret persistence and process teardown, plus Python 3.10/3.12 CI. Verify Linux ARM64 packaging on Jetson 8GB. No native hardware tests have been run yet.

## Rollback

Do not enable the bridge in production. No configuration migration is necessary: leave the flag off and use Alfred's original native Needle/System 2 paths. This branch changes neither `intrinsic_jetson` nor the existing approval semantics.
