# Alfred Phase 2: System 2 reasoning and local approval

Phase 1 (Bluetooth/direct capture, Whistle, Needle, local llama-server, basic tool runtime) is merged on \`main\`. This branch adds schema-grounded Qwen 3.5 planning, shared policy gates and approval handling. The user-confirmed deployment is Jetson Orin Nano 8GB, JetPack 7.2.1, Sony WI-C100 microphone, Qwen3.5-4B Bartowski Q4_K_M GGUF in llama.cpp.

## Design contract

1. **Needle remains System 1.** Simple and explicit read-only operations or reminders can execute immediately via Python. All other actions use the shared \`System2\` preflight and approval policy.
2. **Qwen is System 2.** For complex commands, Needle delegates, or an unsupported simple intent may be answered read-only. Qwen receives actual JSON tool parameter schemas *and* configured approved service/task identifiers. It generates JSON plans through llama.cpp \`response_format.type=json_schema\`.
3. **Grounded follow-up explanations.** For explicitly explanatory requests, Qwen can take a second read-only pass over the completed diagnostic tool results. That pass returns a bounded answer string only: no tools are available, and no side effects can be triggered by the explanation.
4. **Python owns side effects.** Model plans are inert data. Every step undergoes strict allowlist checks, valid argument types and values, safe condition references and service/task permissions *before executing any step*. Tools always execute within the existing user privileges; no arbitrary shell execution.
5. **Local authorization for sensitive writes.** Sensitive actions are staged as pending proposals in SQLite. The user must approve them through a terminal on the Jetson using \`alfred --approve <ID>\`, or reject using \`--reject <ID>\`. The approval CLI is deliberately absent from the model's tool schema and is **never** exposed as a voice command. Default expiry is 180 seconds; approval is single-use.
6. **Default offline-first.** The Qwen server stays loopback-only and is adopted or started as in Phase 1. A Qwen failure must not prevent Needle's basic operations.
7. **Data minimization.** Raw mic audio and plaintext transcripts are not saved by default. Pending proposals store the step arguments until acted on or expired; these may contain personal text (such as reminder content). Keep the local SQLite directory private.

## Permission matrix

| Action | Voice execution | Local CLI approval |
|---|---|---|
| Read-only system/GPU/service status | Yes | No |
| List reminders, workflows and schedules | Yes | No |
| Create one-shot reminder | Yes, explicit Needle command | Qwen-generated plans may execute directly when explicitly delegated |
| Stop/start user service | No | Required |
| Run preapproved executable or script | No | Required |
| Cancel reminder/schedule | No | Required |
| Create recurring status check | No | Required |
| Execute saved workflow | No | Required |
| Save a Qwen-generated workflow | No | Required |

When Needle returns no supported calls, Qwen may answer in *read-only mode*. If Qwen suggests a mutating action in that mode, Alfred refuses it instead of silently removing that step and falsely announcing success.

Needle confidence defaults to 0.7 for direct calls. Uncalibrated (missing) or lower-confidence action selections are refused with a request to repeat or clarify; the threshold is adjustable in config after real Jetson evaluations.

## Local commands

\`\`\`bash
# Pick up the Phase 2 branch:
git fetch origin
git switch feat/phase2-system2-reasoning

# Show pending proposals (no model loading):
python -m alfred --config config.json --pending

# Approve one proposal by ID: the CLI claims it atomically,
# revalidates all steps, then executes once.
python -m alfred --config config.json --approve 1

# Reject one proposal:
python -m alfred --config config.json --reject 1

# Run a Qwen-only planner test on the real local llama-server.
# Any sensitive action will be STAGED, not executed.
python -m alfred --config config.json --reason "Check GPU temperature and explain the result"

# Tests work without Jetson hardware:
python -m unittest discover -s tests -v
\`\`\`

## Safety and limitations

- **Never run Alfred as root.** Local approval is OS-account-controlled CLI access. Anyone who can run Alfred's CLI as that user can approve proposals.
- A multi-step plan is validated before execution, but previously completed side effects are **not rolled back** if a later step fails.
- Claimed-but-crashed approvals stay in \`running\` state and cannot be replayed automatically. This intentionally avoids duplicate side effects; manual inspection is needed if a process crashes.
- Headset voice cannot serve as secure authentication. Bluetooth microphone selection is unrelated to authorization.
- The reasoning model may still make mistakes. Parameter validation and allowlists prevent unknown tools, not all unsafe semantic outcomes of approved tasks.
- This phase implements single-turn plans and statically authorized workflows, not unbounded multi-agent reasoning or full natural-language context memory.
- Pending proposal expiry is enforced on \`--approve\`, but expired records may remain in SQLite for audit.
- No network/cloud fallback when Qwen is unavailable.
- Hardware integration and Jetson performance validation remain outstanding.

## Acceptance gates

1. Portable unit tests cover benign routing, typed parameters, Qwen JSON schema request, preflight refusal, one-time approval, expiry and denial of unsupported actions.
2. On-device \`--doctor\` and \`--ensure-llama\` check the preexisting Bartowski model and process supervision.
3. With Sony WI-C100: Whistle transcripts an English command, Needle picks a tool, no action occurs for uncommitted audio.
4. Qwen can return a valid plan at 4-bit on 8GB without starving continuous audio capture (measure RAM, latency and failure rate).
5. Voice-controlled service stop requests create a proposal, **never immediately stop the service**; local approval performs that action.
6. Confirm/reject from a headset is not treated as approval.

This is a Phase 2 implementation slice, not a claim of hardware validation or completion of every later automation feature.
