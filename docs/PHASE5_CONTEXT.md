# Phase 5 — Issue #16: shared scoped ContextManager

**Status:** opt-in implementation on its own PR from `main` (issue #15 is
still a separate unmerged PR). No native Jetson compatibility or Qwen inference
performance is claimed. Never merge this worker branch directly into
`intrinsic_jetson`.

## Enable or disable

Both flags default to **false**. To enable local in-memory context only:

```json
{"runtime":{"context_enabled":true,
            "context_persistence_enabled":false,
            "context_ttl_seconds":120,
            "context_budget_chars":1200,
            "context_owner":"local",
            "context_mode":"assistant"}}
```

This is a partial config fragment to merge with your existing settings. For
explicitly retained preference facts (unencrypted SQLite), set
`context_persistence_enabled=true` as well, after reviewing the privacy risks.

`context_owner` identifies an explicit local user profile, not a biometric
speaker detector. `context_mode=meeting` uses a separate namespace. Do not
assume microphone speaker diarization/authentication merely from this setting;
external callers must select the correct owner after trusted authorization.
Voice and CLI requests from the same configured owner/mode can use one session.

## Three tiers and model boundaries

1. **Ephemeral task observations:** at most three *verified read-only* tool
   results for that task, held only in memory, discarded at task completion.
   `_bounded_metrics` keeps finite allowlisted numeric/boolean telemetry.
   For `list_services`, only a count can be represented. No output strings,
   shell stdout, transcript, arbitrary JSON, or model-generated action is
   eligible. These may be used by a Qwen continuation in the same task.
2. **Cross-turn session slots:** `gpu_temperature`, `system_status`,
   `active_workflow`, keyed by **(owner, mode, session_id)**. Each slot has
   `observed` provenance, confidence, observation time and a strict
   expires-at TTL (1–300 seconds). Slots are in-memory only even if fact
   persistence is enabled. The workflow slot only says a locally approved
   workflow was referenced; it contains no executable name and NEVER
   authorizes a pronoun action. Unrelated topics invalidate relevant slots.
   `check it again` is resolved locally only when exactly one fresh
   read-only metric is unambiguous; `restart it`, `approve that`, etc.
   are rejected and never sent as implicit privileged tool calls.
3. **Explicitly user-approved facts:** at most eight bounded
   `key → short value` preferences per **owner + mode**, provenance
   `user_approved`, and saved time. No model turn, text or voice command
   can write these automatically; only trusted local CLI (`--memory-set`) or
   a programmatic call with `local_approval=True` does so. Disabled
   persistence stores these only in memory; optional persistence writes to
   the existing SQLite database. Credentials/policy/command-like *keys*,
   oversized values, control characters, obvious shell-like instructions
   and credential patterns are rejected. This is a limited filter, not a
   proof against every prompt injection. These facts are
   **untrusted personal preferences, not policy, approvals or tool authority**.

`ContextManager.compact` applies a deterministic hard JSON budget (256–3200
characters): discard oldest task observations first, then lexically selected
fact entries, then optional slots, never splice JSON mid-token and never
invoke a model to guess missing data. **An optional future local-Qwen summary
needs a separately reviewed, explicitly enabled feature**; issue #16 does
not create summaries or remember model inferences as authority. This is
deliberate: unverified LLM summaries must not silently become trusted facts.

Needle receives only a small typed slot packet when useful, and direct
pronoun resolution happens in Python **without any Qwen call**. Qwen and the
optional pinned Harness planner receive the same bounded packet as
non-authoritative data, alongside the ORIGINAL user request; they cannot
rely on remembered measurements to bypass the existing System2 preflight or
`--approve` checks. The existing #13/#14 approval, no-replay and
wake-authorization protections remain unchanged. This branch is built on
merged #14, not on unmerged #15; both PRs may require a focused integration
rebase when #15 is approved.

## Privacy, lifecycle and deletion

- No audio samples, transcription histories or meeting contents are written
  by the new context tables. `ContextManager.start_task/finish_task`
  manages transient observations; `clear_session(session_id)` deletes
  session slots and transient task observations, preserving only explicitly
  approved owner/mode facts. The wake-authorized/local text command
  **`clear this session`** performs the same cleanup without invoking
  Needle or Qwen; it never deletes approved facts. `Alfred.close` clears
  the current session.
- Restart loses session slots and task results (fresh observations must be
  obtained again). Explicit facts persist across restart **only** when both
  context flags are enabled. There is no implicit memory-to-action replay.
- The existing SQLite DB file is chmod 0600 when fact persistence is
  enabled and must be an owned regular file. SQLite WAL/SHM files are also
  restricted when present. This does **not** encrypt files, backups, swap,
  system snapshots or SSD discarded blocks.
- For trusted local controls (never model/voice):
  ```sh
  alfred --memory-set answer_style=concise
  alfred --memory-list
  alfred --memory-clear answer_style
  alfred --memory-clear ALL
  ```
  All require `context_enabled=true` and
  `context_persistence_enabled=true`. Changing `context_owner` or
  `context_mode` isolates the view. On-disk deletion is a logical SQLite
  deletion; secure physical erasure of filesystem backups, WAL history,
  removable flash, etc. is **not** guaranteed. An encrypted-at-rest option
  should be specified in a separate threat-modelled follow-up if necessary.
- Important existing caveat: the preexisting `Store.event("tool")` audit
  path may record tool arguments/results outside `ContextManager`. The
  new context does not globally scrub historical tool logs. Never claim
  the complete Alfred DB is zero-retention; deploy accordingly.

## Security and test cases

Do **not** use `ContextManager.observe` with model-output JSON. Only the
trusted System2 result path calls it. An attacker with the ability to
execute arbitrary Python in Alfred's process already owns the context
authority; this code is not an OS security sandbox. User-provided facts
are never privileged instructions. Context owner is a configured namespace,
not cryptographic user identity or automatic voice identification.

Acceptance tests in `tests/test_context.py` cover TTL equality/freshness,
ambiguous read-only references, action-pronoun rejection, topic invalidation,
distinct sessions/owners/meeting mode, explicit fact deletion/approval,
database permission/restart, concurrent task windows, malicious result
injection, missing/forged/low-confidence readings, deterministic size budget,
Needle no-Qwen fast path, optional Qwen context packet and defaults.
Existing #13/#14 Python 3.10/3.12 and pinned Harness SDK/Cordis smoke
are inherited through CI.

Example offline test:
```sh
python -m unittest discover -s tests -v
```

No new model inference or networking is required when context is disabled.
On the real device, validate P50/P95 against the baseline with the relevant
feature enabled and disabled before production rollout. Hardware acceptance
[#29](https://github.com/Siddharthgolecha/Alfred/issues/29) is not
satisfied by offline Python mocks.

SQLite uses `PRAGMA secure_delete=ON` when persistent facts are enabled, but this does not erase backups, filesystem snapshots or all historical WAL data. Treat disk retention as sensitive even after logical deletion.
