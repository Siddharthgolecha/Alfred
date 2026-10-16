# 3. Approve sensitive actions only from the local CLI

**Status:** In effect

## Context

A voice interface can be spoofed by background audio, a recording, or a model that produces a
convincing instruction. Approval must be something none of those can perform.

## Decision

Only a fixed set of read-only tools and simple reminders runs directly on a voice command. Every
other tool, and every plan saved as a workflow, is staged as a pending approval. It runs only
after `alfred --approve ID` from a local terminal.

Approval is one-shot, expires after `runtime.approval_ttl_seconds`, and the plan is validated
again at execution. `--approve`, `--reject`, `--pending`, `--memory-*` and `--workflow-*` are CLI
operations. They are not in any tool schema, so neither Needle nor Qwen can call them.

## Consequences

- A spoken "approve it" cannot approve anything.
- Privileged actions need a person at the terminal. That is intentional friction.
- Workflows need two local steps: approve the graph, then start it.

## Where

[`alfred/system2.py`](../../../alfred/system2.py) (`VOICE_EXECUTABLE_TOOLS`,
`APPROVAL_REQUIRED_TOOLS`, `Approvals`), [`alfred/cli.py`](../../../alfred/cli.py). See
[Tools](../../reference/tools.md) and [Security model](../security-model.md).
