# Phase 5.1 / Issue 58: Required DeepSeek Harness foundation

**Current state:** The official pinned SDK and Cordis runtime are mandatory
for Qwen cognition; missing/unsafe SDK is a startup failure, with no independent
Qwen planner fallback. Fresh config enables Needle feedback, context, bounded
escalation and resource admission. Effective Cordis plugin graph remains audited
at startup.

**Outstanding:** The restricted Cordis profile is still tool-free. Native
Needle's feedback loop remains Python-managed, not Cordis-owned. This foundation
PR does **not** satisfy the shared Cordis task/tool/observation acceptance
criteria, or native Jetson #18/#29 gates. Do not close Issue #58 on this PR.

Normal install: `python -m pip install -e .`.
For offline Jetson x86_64/aarch64, obtain the exact pinned wheel files from
`coordination/deepseek-harness-lock.json`, then run
`python scripts/install_harness.py --wheelhouse /path --verify` and
`python scripts/install_harness.py --wheelhouse /path --install`.
Wheel hashes are checked **before** install; the installed package version
and actual Cordis graph are checked at runtime. Dependencies beyond those two
wheels must be pre-provisioned offline.

## Original Phase 5 opt-in SDK implementation (historical)

# Issue #13 — optional offline DeepSeek Harness SDK bridge

**Status:** Implemented as a read-only *Cordis runtime* that can propose actions, never execute them. Disabled by default. **No shell, plugin, tool, browser, remote endpoint, or automatic approval is ever authorized by the Harness runtime.** Alfred's existing System2/Tools preflight and one-time approvals exclusively execute/stage validated proposals. Native Needle stays in Python, sharing the canonical session ID with SDK Qwen. The existing Qwen-compatible inference service is shared, not restarted by the SDK.

## Upstream identity and reproducible packages

- Upstream: [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness); source tree examined at commit `571902028a75c64c1d767f4ae5fd099485f0ddf0` (2026-10-14). **This is an audit reference, not a claim that the published SDK is built from this commit.**
- Official published SDK: `deepseek-harness-sdk==0.1.5rc1`, SDK universal Python wheel SHA-256 `8d9897e97e8a95fd738cb2c3e2c563951d824e43847ec6f3b0fb172c0729b4d8`.
- Matching runtime wheel: `deepseek-harness-runtime-bin==0.1.5rc1`.
- Linux x64 (glibc ≥2.28): SHA-256 `ad877237382baff4628969a9ebdbfe38486d971b7987c032d0e29775759b5229`.
- Linux ARM64/aarch64 (glibc ≥2.28): SHA-256 `32b7bb7887ea89f12462274d1b21acfafb00d38d94e11a5d7122492f185e4f85`.
- Canonical checked manifest: `coordination/deepseek-harness-lock.json`. Official PyPI release: [SDK](https://pypi.org/project/deepseek-harness-sdk/0.1.5rc1/), [runtime](https://pypi.org/project/deepseek-harness-runtime-bin/0.1.5rc1/). This is a **prerelease**; no floating `main/master/latest` reference is installed.

The SDK package contains the upstream Python JSON-RPC client. The runtime wheel bundles the executable `dsh` and the Core Cordis tree. Python SDK `DeepSeekHarness` communicates with its owned child over SDK JSON-RPC stdin/stdout. Alfred does **not** substitute its internal frame format for that wire protocol.

## Safe Cordis composition

The stock `sdk-minimal` profile includes persistent Bash/PowerShell with `danger-full-access`, JSONL session history, and potentially other content services. **Never invoke it without Alfred's additive safety patch.** `alfred/cognitive/alfred_safe.patch.yml` explicitly disables BOTH persistent shell tools, BOTH terminal backends, subprocess and PTY, sandbox, filesystem/image and working-directory capabilities, MCP resources, jobs, plugin inventory, automatic compaction, and DeepSeek session-log uploads. The tool registry remains present but empty. The bundled JSON-RPC server, agent core and local model adapter remain available. `check_safe_patch()` rejects any new/modified/duplicated row or incomplete disable list before launch, and detects changes between calls.

No external plugin can be installed by Alfred; no URL beyond loopback is accepted. Arbitrary `profile`, `patches`, `dsh_bin` and SDK environment overrides are **not** exposed in Alfred's config. The local provider is initialized with `base_url=http://127.0.0.1:PORT/v1` (or validated localhost/IPv6 loopback) and an inert local-only credential. SDK runtime is **never** instructed to start a llama.cpp/Ollama/LM Studio server. Existing `ModelSupervisor.ensure_running()` remains the only server lifecycle authority.

A private `0700` temporary Harness home and workspace are created per Alfred instance. Although the SDK writes temporary JSONL history while the optional feature is being used, this home is removed on normal completion, close, or exception, and no transcript enters the Alfred `cognitive_*` tables. **Opting in does temporarily persist text on local disk**; consider enabling full-disk encryption and disk cleanup guarantees on Jetson. The default flag stays off to preserve the privacy posture.

## Execution behavior, approvals and shared session IDs

With `runtime.harness_enabled=false` (the default), Alfred is identical to main.

When enabled:

1. Wake-authorized/CLI command opens its existing canonical `TaskEnvelope` and logical `session_id`; native Needle routes it. Direct commands, including GPU/system status, retain the existing `System2.direct` policy and **do not make an SDK/Qwen request**.
2. For unsupported, **read-only** questions (Needle produced no calls), Alfred uses `LocalHarness.run(text, session_id)`. The actual official SDK starts one lazily owned subprocess, reuses it and forwards a single prompt to the existing loopback Qwen endpoint. The same canonical session ID is used for subsequent read-only turns, while the event ledger keeps task IDs per authorized request.
3. For action-capable Needle delegation or `--reason`, the same SDK session produces an **untrusted JSON plan** using a bounded tool catalog. Alfred then calls its existing `parse_plan` (complete schema and preflight checks) and `System2.execute_or_stage` (existing approval policy). The SDK itself has zero Cordis tools; plans are not executed in Cordis. A malformed/unsafe plan is **never executed**: it now **fails closed** without replanning. A missing/crashed SDK can fall back to legacy `System2.reason` **only before any plan is executed or staged**. `System2.execute_or_stage` sits outside the retry handler, so a partial side effect or approval-stage exception cannot be replayed. No call is made to `System2.approve` without a separate local CLI approval.
4. `alfred/cognitive/harness_bridge.py` is a separate *internal structural frame* adapter for canonical TaskEnvelope, Decision, Observation, Escalation, ApprovalReference, Completion and Diagnostic contracts. It strictly validates shapes and sizes and **cannot contain executable tool arguments or trusted observations**. It is not the SDK's official JSON-RPC protocol; the SDK uses its own typed `DeepSeekHarness` client.

**Limit:** Cordis does not host Alfred's privileged tools or native Needle model. All Needle observations and actionable Qwen plans remain inside Alfred's Python orchestration, correlated with the SDK's same session ID. Cordis returns text/proposals only. Extending Cordis to call tools directly would require a separate security review and is intentionally outside this issue.

## Installation

```bash
# Python 3.10+; Linux x64 or ARM64 with glibc >= 2.28.
# Download wheels in a connected trusted environment, verify each SHA-256
# against coordination/deepseek-harness-lock.json, then install from local files.
python -m pip install "pydantic>=2.12,<3"
python -m pip install --no-deps \
  ./deepseek_harness_runtime_bin-0.1.5rc1-py3-none-manylinux_2_28_aarch64.whl \
  ./deepseek_harness_sdk-0.1.5rc1-py3-none-any.whl
```

For x64 replace the runtime wheel filename with `manylinux_2_28_x86_64`. Offline Jetson deployment: transfer *verified* wheels (including matching `pydantic` dependencies) and install with `--no-index --find-links /path/to/wheelhouse`. Never run an unverified `pip install -U deepseek-harness-sdk` or download arbitrary plugins. Ensure Qwen already runs on a loopback OpenAI-compatible endpoint and no cloud API key or external adapter is selected. Existing Ollama/LM Studio/llama.cpp HTTP endpoints remain valid; some backend-specific chat/streaming behavior may vary, so validate each backend's compatibility on device.

Enable deliberately in a local `config.json`:

```json
{"runtime":{"harness_enabled":true,"harness_timeout_seconds":45}}
```

Keep the rest of the normal Alfred configuration and allowlists unchanged. If the SDK is missing, it is not auto-installed, and read-only queries fall back to the prior System2 Qwen path. `harness_enabled` only impacts optional read-only queries; it does not install or advertise new voice tools.

## Reproducible evidence

CI `.github/workflows/check.yml` runs full offline Python 3.10 and 3.12 regression suites. A dedicated `harness-sdk-smoke` job downloads both Linux release wheels plus ARM64 artifact from PyPI and **verifies all three SHA-256 hashes before installing**. Then `tests/sdk_smoke.py` starts a real pinned Cordis/SDK process, talks to a threaded local stub model by streaming OpenAI-compatible SSE, confirms a normal reply **and a same-session structured Qwen plan**, and asserts both model requests contain **no model-facing Cordis tools**. It asserts the SDK child is reaped after close. All SDK traffic in the smoke stays on `127.0.0.1`; no real DeepSeek or Qwen model is downloaded.

`tests/test_harness_bridge.py` covers strict canonical schemas, duplicate keys, size bounds, spoofed tool/URL/plugin payload rejection. `tests/test_harness_sdk.py` covers URL and configuration rejection, lifecycle and reuse, timeout, SDK crash fallback, shared canonical session, empty tool authority, and unchanged System2 direct actions.

The CI smoke proves the x64 runtime starts without tools and can reach a local model shim. The **ARM64 wheel SHA-256 is checked but actual hardware compatibility, speed/memory and service behavior require Jetson 8GB testing.** Python 3.10/3.12 portable checks do not prove native Jetson audio support. Runtime package may have transitive dependencies; only official SDK and platform runtime wheels are hashed in this issue's manifest.

## Rollback

Set `harness_enabled=false` (or remove that optional key) and restart Alfred. The SDK subprocess shuts down; its temporary home is deleted. No schema rollback is required; all old SQLite tables and existing approvals remain unmodified. Neither `main` nor `intrinsic_jetson` are merged by Worker A.

## Independent security review packet

See [`docs/HARNESS_PR27_REVIEW.md`](HARNESS_PR27_REVIEW.md) for complete base-to-head patch links, uploaded GitHub Actions source-diff artifacts, live effective Cordis plugin permission inventory, no-replay regression evidence and residual threat boundaries. Every owned runtime startup now performs a boot-free **effective-configuration plugin graph audit**, not merely a static YAML syntax check; unknown or reenabled plugin rows stop startup.
