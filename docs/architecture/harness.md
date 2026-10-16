# DeepSeek Harness

Alfred hosts Qwen planning through the **DeepSeek Harness SDK** and its bundled **Cordis**
runtime. The Harness is a required dependency. If it is missing, the wrong version, or fails its
audit, interactive Alfred does not start and does not fall back to a different planner.

:::{important}
**Scope of the current integration.** Cordis runs with an *empty* model-facing tool registry. It
returns text that Alfred parses as an untrusted plan. Needle and its bounded feedback loop still
run in Alfred's own Python process, not inside the Cordis session. Shared Cordis task lifecycle
and Jetson hardware validation are not yet done (tracked as issue #58 follow-ups and #18/#29).
:::

## How the pieces connect

```{mermaid}
flowchart LR
    subgraph alfred["Alfred process"]
        sys2["System2"]
        lh["LocalHarness<br/>harness_sdk.py"]
        audit["harness_audit.py<br/>check_safe_patch<br/>audit_effective_cordis_config"]
        cfg["harness_config.py<br/>pinned plugin manifest"]
        patch["alfred_safe.patch.yml"]
    end
    subgraph child["Child process (owned by SDK)"]
        dsh["dsh / Cordis<br/>profile sdk-minimal + patch"]
    end
    srv["Local inference server<br/>(loopback, /v1)"]

    sys2 -- "plan(request, tools, handoff)" --> lh
    lh -- "audit before launch" --> audit
    audit --- cfg
    audit --- patch
    lh -- "JSON-RPC over stdio" --> dsh
    dsh -- "HTTP chat completions" --> srv
```

Alfred does not start the model server for the Harness. `ModelSupervisor` owns that, and the SDK
is simply pointed at the same loopback URL with `base_url = <url>/v1`.

## Pinned versions

| Package | Version | Pinned in |
|---|---|---|
| `deepseek-harness-sdk` | `0.1.5rc1` | `pyproject.toml`, `harness_config.SDK_VERSION` |
| `deepseek-harness-runtime-bin` | `0.1.5rc1` | same |

Wheel SHA-256 checksums for the SDK and for the Linux x86_64 and aarch64 runtimes live in
[`coordination/deepseek-harness-lock.json`](../../coordination/deepseek-harness-lock.json). CI
re-downloads the wheels and compares hashes (`harness-sdk-smoke` job). See
[Installation](../getting-started/installation.md) for the offline installer.

A normal `pip install` cannot be verified retroactively against the published hashes. Verify
wheels *before* installing them, or keep the verified artifacts.

## What happens before any model turn

1. **Overlay check.** `check_safe_patch` reads `alfred_safe.patch.yml` and requires exactly the
   19 entries in `DENIED_ROWS`, each `disabled: true`, no extra rows or syntax (comments are
   ignored), and a size under 4096 bytes. It returns the file's SHA-256. If the file changes later, Alfred
   refuses to continue ("Cordis overlay changed").
2. **Version check.** `importlib.metadata` must report `0.1.5rc1` for both distributions.
3. **Effective-config audit.** Alfred runs the bundled runtime with
   `--profile sdk-minimal --patch <overlay> --dump-config` in a throwaway directory. The
   `audit_effective_cordis_config` parser reads only literal top-level `id`, `name` and `disabled`
   fields. It never evaluates `!!js` tags or YAML constructors. The set of rows must match
   `EXPECTED_ACTIVE_ROWS` (20 plugins) plus `EXPECTED_DISABLED_ROWS` (11 plugins) exactly.
4. **Admission.** The audit and the SDK startup each run under
   `InferenceAdmission.claim("harness")`. They are not nested with a Qwen claim.
5. **Launch.** `DeepSeekHarness(...)` starts one child process with a private `0700` home and
   workspace under a temp directory, profile `sdk-minimal`, the patch, and the exact model ID the
   backend advertises.

Why the overlay is needed: the default `sdk-minimal` profile would enable shell and terminal
plugins. The overlay disables shells, PTY, subprocess, sandbox, filesystem, attachments,
working-directory, MCP resources, jobs, package inventory, session log, compaction and image
offload.

## The planning contract

`LocalHarness.plan()` builds a bounded prompt (at most 8192 characters) and sends it through the
SDK. The system prompt tells the model it has no tools, that it may only *propose* typed steps,
and that a proposal is not execution or approval.

The reply must be a single JSON object with exactly `message`, `steps` and `save_as`. Alfred
rejects duplicate keys, non-finite numbers, extra or missing fields and responses over 2000
characters (`HarnessInvalidPlan`). Even then the plan is only a candidate:

```{mermaid}
flowchart LR
    reply["SDK reply (text)"] --> json["JSON shape check<br/>LocalHarness.plan"]
    json --> parse["System2.parse_plan<br/>schema, allowlists, step limit"]
    parse --> recon["EscalationEnvelope.reconcile<br/>(handoffs only)"]
    recon --> pol["System2.execute_or_stage"]
    pol --> run["Run read-only steps"]
    pol --> stage["Stage sensitive steps<br/>for alfred --approve"]
```

Tool schemas sent to the model include only argument names and primitive types. `delegate_to_reasoner`
and `run_workflow` are never offered.

## Failure handling

| Condition | Behaviour |
|---|---|
| Endpoint is not loopback HTTP with an explicit port | `HarnessUnavailable` at construction |
| Model ID does not match the allowed pattern | `HarnessUnavailable` |
| SDK or runtime missing or wrong version | `HarnessUnavailable`; interactive start fails |
| Plugin inventory differs from the pinned manifest | `HarnessUnavailable` |
| Turn exceeds `runtime.harness_timeout_seconds` (5–120, default 45) | The SDK child is closed and `HarnessUnavailable` is raised |
| Reply is not a valid plan | `HarnessInvalidPlan`; no retry through another planner |
| `finish_reason` is not `completed`, or response too long | `HarnessUnavailable` |

`HarnessUnavailable` is a `RuntimeError` subclass in `harness_audit.py`.

## Privacy

The SDK writes session JSONL files inside its private `0700` temp directory while running. Alfred
removes the directory on `close()`. This is not secure erasure after a power loss or `SIGKILL`.
Use disk encryption if that matters. The API key passed to the SDK is the constant
`alfred-local-only` and is never a real credential.

## Checking it

```bash
python -m alfred --config config.json --doctor
```

prints the plugin audit result, then, if the backend is ready, starts a real SDK child and
reports `Official Cordis SDK subprocess: READY (restricted/tool-free)`. It also prints
`Cordis tool bridge: NOT YET WIRED`, which is expected.

Tests:

| Test | What it proves |
|---|---|
| `tests/test_harness_sdk.py`, `tests/test_harness_required.py` | Policy, audit logic and fail-closed paths |
| `tests/sdk_smoke.py` | A genuine pinned SDK and Cordis process against a canned local HTTP stub: plugin inventory, prompt contract, no tools |
| `tests/sdk_live_qwen_probe.py` (opt-in) | A real local Qwen produces a read-only typed plan |

The smoke test cannot show planning quality, because the stub returns fixed JSON.

## Upgrading the SDK

An upgrade is a deliberate change, not a dependency bump. Update `SDK_VERSION`, the manifests in
`harness_config.py`, the overlay, the pins in `pyproject.toml`, and the hashes in the lock file,
then re-run the smoke test. The audit will fail closed until all of them agree.

## Migrating from the earlier opt-in preview

`runtime.harness_enabled` no longer exists, and a config that still contains it is rejected as an
unknown key. There is no switch to disable the Harness for interactive planning.
