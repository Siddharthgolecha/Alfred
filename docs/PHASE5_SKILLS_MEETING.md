# Issue #17 — Skills inventory and offline meeting contract (main only)

**Status:** interface-only, opt-in by trusted host injection, no Jetson code
copied to `main`. Built from `main` commit
`9db79805558e1fd4b2d81df13de539662ef99258`, not stacked on
unmerged #15 (PR #32) or #16 (PR #33). **Final acceptance/merge is blocked**
until those dependencies merge and both integration boundaries are reviewed.

## Public capabilities and discovery

Run `python -m alfred --skills` to inspect the typed seven-skill inventory
without native Needle, Qwen, microphones, SSD or network. Each entry reports
`available`, reason, interface version and consent/local-approval requirements:

| Skill | Backing detection | Execution authority |
|---|---|---|
| `gpu_status` | `tegrastats` detected | Existing `get_gpu_status` / System2 |
| `health_graph` | actual *approved* Phase 4 graph named `health_graph` | Phase 4 digest plus trusted local approval |
| `reminders` | live local Store + Automations objects | Existing restricted reminder tools |
| `meeting_recording` | provider attests microphone, guarded SSD and recorder | Only trusted local/authorized voice with consent |
| `transcript_retrieval` | provider attests guarded SSD and transcription | Explicit meeting ID, per-task consent and scope |
| `qwen_minutes` | provider attests local Qwen, transcription, safe PDF | Structured evidence, no model-controlled output path |
| `qr_obex_handoff` | provider attests safe PDF and QR/OBEX | Separate local authorization for delivery |

**A skill is metadata, NOT a newly granted tool.** `alfred/skills.py`
adds no `TOOL_SCHEMAS`, import hooks, module search, shell, arbitrary URL,
file reader, plugin startup or new ACL bypass. Absent hardware/optional
bindings are correctly **unavailable**; the interface does not interpret
branch names as device evidence.

`Alfred(..., meeting_adapter=None)` defaults to no meeting capabilities.
Device integrations may pass an explicitly constructed
`OfflineMeetingAdapter(provider)` from trusted code; unknown plugin
packages are never dynamically imported on main. No automatic meeting
recording or transcript search is wired into audio, Qwen or Needle.

## Trusted Jetson compatibility contract (AFTER main merge)

A Jetson-only follow-up PR against `intrinsic_jetson` should adapt
existing `alfred/jetson/capture.py`, `transcription.py`,
`minutes.py`, `storage.py`, `transfer.py` and associated CLI without
merging their device-specific implementation back into main.

The host-owned provider must expose:
- `capability(feature) -> bool` for each exact primitive:
  `microphone`, `guarded_ssd`, `recording`, `transcription`,
  `qwen`, `safe_pdf`, `qr_obex`. Return **false** if unverified.
- `start_recording(consent=True, retention=bool) -> opaque meeting_id`,
  `recording_status(meeting_id)`, `stop_recording(meeting_id)`.
  When retention is **false**, `discard_meeting(meeting_id) -> True`
  must confirm device-side deletion; otherwise the main adapter raises
  rather than falsely report that private recording data was discarded.
- `get_segments(meeting_id) -> current authorized segments`, never a
  historical meeting list or filesystem search.
- `suggest_minutes({task_id,meeting_id,untrusted_evidence,policy}) ->
  extractive cited proposal`, `save_pdf(meeting_id, normalized_minutes,
  segments) -> opaque PDF handle`, and `deliver_pdf(meeting_id,handle)`.
  **Do not** accept paths from a model. The existing GuardedSSD and
  `save_meeting_pdf` remain authoritative for storage, PDF escaping and
  protected destinations. The real meeting summarizer currently returns
  unreferenced free-form minutes; the future adapter must constrain the
  planner schema and provide segment citations rather than silently
  upgrading those free-form claims to verified facts.
- No provider should claim synchronization accuracy beyond existing
  decoder-estimated word/window timing and per-device calibration.
  Channel label **is not a verified speaker identity**.

No real Jetson provider is installed or executed by this main PR.

## Scope, privacy and output rules

Start requires an authorized task plus **explicit recording consent**,
and separate `retention` flag. Every operation on a recording requires a
new **task-bound**, time-limited `MeetingPermit` with the exact canonical
session ID, owner and *opted-in meeting ID*. Never enumerate historical
transcripts or try to derive an ID from general session context.
Meeting/permit/validated PDF metadata is in-memory, not persisted to
cognitive facts, and `clear_session` revokes permits/handles. With
retention disabled, stopping the recorder revokes transcript access.
An owner/scope mismatch or expired/revoked/forged permit is denied.

`sanitize_segments` preserves source mic channel, UTC start/end timestamps,
overlap references, optional bounded per-word decoder-estimated timing,
ambiguous and likely cross-talk flags, and sync uncertainty; it forces `identity_type=microphone_channel` and **never**
promotes a device to a real person's identity. All text is size-bounded
and known obvious tokens/emails are redacted. Max 64 segments/16k evidence
JSON bytes. Redaction is **best effort**, not a guarantee against every
credential string; device adapters must follow retention/data policy.

`validate_minutes_proposal` enforces a *citation + verbatim quote* from a
current sanitized segment for **every summary, point, decision, risk, question
and action item**. Due dates must appear explicitly as ISO dates in the
cited excerpt or be `Not specified`. Owners must occur in that quoted
excerpt or be `Unassigned`. Mic channel labels cannot be owners. No
invented attendance or speaker identity fields are allowed. Qwen's
generated output is untrusted until this validation succeeds. Only the
validated exact digest *and matching cited evidence snapshot* may be sent
to the guarded PDF writer; a changed transcript must be revalidated.
QR/OBEX
delivery additionally requires a separate trusted local confirmation, and
must never report delivery success when the device returns `delivered=false`.

The original meeting transcript itself can contain prompt injection.
Extractive quotes can reproduce malicious words **as user content** and are
not command authority. This module does not promise to determine whether
a quoted statement is true, only that it was present verbatim. The model
cannot use quoted content to acquire shell, file, network, approval or
workflow privileges.

## System 1 / System 2 and workflow boundary

- System 1 may discover `meeting_recording` and match a trusted
  start/stop/status **intent**, but cannot invoke provider methods without
  the local wake/CLI authorization and matching per-task permit.
  This main-only PR adds no voice tool activation for an absent device.
- System 2 may receive bounded, sanitized timestamps/uncertainty, mic
  sources and citeable excerpts for proposals concerning MoM and
  disagreements. It does **not** receive a general transcript/memory
  search or an executor. Decisions/dates/owners are validated after
  inference, before any guarded PDF writing.
- Existing Phase 4 `WorkflowEngine` is unchanged; approved graph digest
  and service/named-task allowlists continue to gate all actions. Skill
  metadata does not make a workflow executable or locally approved.
- When #15/#16 are merged, rebase and integrate the **consented meeting
  evidence packet** only into the appropriate same-task context, never
  global retained session slots. No transcript read without explicit ID.

## Fake-device acceptance and hardware follow-up

```sh
python -m unittest tests.test_skills_meeting -v
python -m unittest discover -s tests -v
python -m alfred --skills
```

The fake provider tests cover missing devices, real graph approval status,
consent/retention with device discard verification, owner/task/session
mismatch, expiry/revocation, opaque handles, redaction, timestamp/
overlap/cross-talk, post-validation evidence mutation, hallucinated
owners/dates/decisions, PDFs with modified content, failed OBEX delivery,
and no module import/execution side effects on main.

Jetson-only compatibility PR should test **actual microphone enumeration**
and per-channel capture with USB/Bluetooth, SSD ownership and free space,
bounded/offline Whistle transcription with overlap/cross-talk, exact UTC
timing and calibration uncertainty, safe PDF generation/escaping, Qwen
claim citation audit, QR/OBEX transfer completion (not just pairing),
recording stop/cancellation, retention deletion and low-memory behavior.

On-device checklist: physical 8GB Jetson, 1–N USB microphones via hub,
Bluetooth microphone if enabled, transcript overlap checks, source-channel
(not real-speaker) labels, guarded SSD capacity, no public network
dependencies, offline Qwen/Whistle, PDF open-and-review, explicit consent
start/stop/read/transfer, 100% local approval gating, P50/P95 CPU/RAM/
power/thermal baseline, and no background child process after shutdown.

**Hard blockers:** #15 and #16 are unmerged; Jetson implementation is
absent from main by design. Passing fake tests does NOT mean end-to-end
Jetson meeting recording, real Qwen MoM or QR/OBEX works.
