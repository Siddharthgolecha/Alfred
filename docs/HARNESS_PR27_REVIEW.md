# Security review evidence for PR #27 (issue #13)

This is the independent review packet requested in the Maestaris PR comment.
**Not merge authorization.** Reviewer must verify the actual current PR head,
CI conclusion, policy surface and own security risk judgement before merging.

## Full auditable patch

- Canonical full base-to-head patch: https://github.com/Siddharthgolecha/Alfred/pull/27.diff
- Canonical per-commit patch series: https://github.com/Siddharthgolecha/Alfred/pull/27.patch
- GitHub's pull-request Files changed tab: https://github.com/Siddharthgolecha/Alfred/pull/27/files
- CI **review-base-to-head** job runs `git diff --binary "$BASE_SHA" "$HEAD_SHA"`
  on the actual `pull_request.base.sha` and `pull_request.head.sha`, uploads
  `full-base-to-head.diff`, `paths.txt`, `summary.txt`, `revisions.txt`
  and SHA-256 `SHA256SUMS` as a GitHub Actions artifact named
  `review-base-to-head-27-<head SHA>` for 30 days. The source is **not** a
  last-commit patch; verify revisions.txt against the actual reviewed head.
- GitHub connector `fetch_pr_patch(repo_full_name=...,pr_number=27)` returns
  the full paged series of changed-file patches. Use it rather than a single
  commit's `/commits/{sha}` patch.

## Exactly what gets executed and when

| Boundary | Authority | Safety condition | Validation |
|---|---|---|---|
| Wake/transcription/Needle | `WakeWordGate`, `Cactus.route` | unauthorized voice never becomes a task; no Shell SDK call from Needle | existing wakeword and routing regressions |
| SDK startup | `LocalHarness._open` | pinned exact `sdk/runtime==0.1.5rc1`, private `0700` home, audited immutable local Cordis patch, fresh official `--dump-config` effective graph comparison **before launching SDK** | `test_patch_is_allowlisted_disable_only`, `test_exact_effective_cordis_audit_rejects_bypass_and_spoof`, live `tests/sdk_smoke.py` |
| SDK inference | `DeepSeekHarness` over SDK JSON-RPC | only loopback HTTP model API, **zero model-facing Cordis tools**, no second llama/Ollama/LM Studio process | `test_read_only_model_turn_and_teardown` with real pinned binary + adversarial prompt and local SSE model |
| Model plan | `LocalHarness.plan` returns untrusted JSON | no tool executed by Cordis; `parse_plan` enforces complete schema, known names and concrete service/task allowlists; invalid plan is **rejected**, not replayed or delegated | `test_harness_tool_spoof_cannot_skip_system2_preflight` |
| Side effects | existing `System2.execute_or_stage` | **outside all SDK fallback exception handlers**; exception after partial side effect must surface as failure without a second execution | `test_execution_exception_cannot_replay_after_partial_side_effect` |
| Sensitive approvals | `System2.approvals.propose`, `System2.approve` | request stages a durable one-shot pending ID; no remote/model invocation of `approve`. Trusted local `--approve ID` only, no change to `approve` semantics | `test_sensitive_plan_staged_once_and_not_auto_approved`, existing `tests/test_system2.py` |
| Crash before execution | `reason()` | fallback to existing System2 only during SDK startup/prompt generation. Fallback cannot occur after `parse_plan` or `execute_or_stage`. A failure may still cause another **planning** inference, not another already-run side effect | `test_generation_crash_may_fallback_before_any_execution`, `test_sdk_crash_falls_back_without_new_tool_grants` |
| Cleanup/logging | `LocalHarness.close` | owned SDK child reaped, temporary JSONL home deleted on close, no raw text in Alf​red cognitive tables or raised error string | `test_runtime_exception_does_not_echo_secret_or_retain_temp_home`, live SDK smoke |

## Effective Cordis permission inventory (official wheel, *not* source guesswork)

The official published runtime's CLI supports this **boot-free** inspection, in
the exact private home and with the same CLI patch as the real SDK child:

```sh
DSH_HOME=/tmp/alfred-isolated-home dsh --profile sdk-minimal \
    --patch /path/to/alfred/cognitive/alfred_safe.patch.yml --dump-config
```

CI invokes the binary from `deepseek_harness_runtime.resolve_bundled_launch_args()`
rather than a possibly different system-installed `dsh`. Runtime startup now
repeats that audit on every newly-owned SDK process. The dumped top-level row
IDs, *package names* and literal `disabled` flags must match exactly
`EXPECTED_ACTIVE_ROWS` / `EXPECTED_DISABLED_ROWS` in
`alfred/cognitive/harness_sdk.py`. Missing/unknown/duplicate rows, unexpected
plugin packages, dynamic/false disable flags, shell re-enablement and home
profile injection fail closed.

**Expected disabled native rows (11):**
`session-log-deepseek`, `plugin-package-inventory-deepseek`, `sandbox`,
`sandbox-policy`, `subprocess`, `pty`, `terminal-bash`, `terminal-pwsh`,
`jobs`, `persistent-bash`, `persistent-pwsh`.

**Expected allowed infrastructure (20):**
`sdk-app-startup`, `sdk-jsonrpc-server`, `deepseek-llm-api-extensions`,
`llm-deepseek`, `session-projection`, `timer`, `llm`, `session`,
`session-title`, `system-prompt`, `tools`, `agent`, `llm-retry`,
`invariants`, `session-invariant`, `agent-invariant`, `scope-invariant`,
`agent-loop-invariant`, `agent-loop`, `sessions`.

The patch itself names additional explicitly disabled rows absent from the
pinned 0.1.5rc1 effective tree (e.g. working-directory, filesystem, MCP
resources). They do not introduce active plugin entries. Any future release
that actually inserts them will fail the exact version/inventory lock until
independently reviewed.

The only active `tools` row is a **registry**, not a model-facing tool
implementation. The real SDK test asserts `tools` is absent or empty in
*both* actual model HTTP requests, including a malicious instruction to run
a shell. Disabled session-log uploader is distinct from the active **local
JSONL session persistence**, which temporarily retains prompts in `0700`
private home while opted in and is deleted on clean shutdown. Neither model
prompt text nor tool arguments are saved to Alfred's cognitive ledger.

## Boundaries and non-claims

- This tests **plugin composition and model-advertised tools**, not a kernel-enforced network or filesystem sandbox. The official runtime subprocess is still an OS process with its caller's basic privileges; it can write its intentionally local private JSONL store and connect to the configured local HTTP model. It is never granted user-facing Cordis shell, filesystem or network tools.
- SDK output can be wrong, malicious, or inaccurate; only System2 parses, validates, stages, and (for permitted read-only steps) runs it.
- Profile and wheel checks apply to tested pinned Linux x64 binary; the ARM64 wheel SHA-256 is checked in CI but **not executed on Jetson**.
- No guarantee of surviving abrupt power loss/kill before private JSONL cleanup. For strict no-temporary-text-on-disk policies, leave `harness_enabled=false` (the default).
- Native Jetson memory, performance and non-llama.cpp backend streaming require separate on-device checks; do not merge or declare production readiness from this packet alone.
