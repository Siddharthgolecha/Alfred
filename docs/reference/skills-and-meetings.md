# Skills and meetings reference

This page covers two related, **device-neutral** interfaces:

- The **skill registry** ([`skills.py`](../../alfred/skills.py)): a read-only inventory of
  capabilities.
- The **offline meeting adapter** ([`meeting_adapter.py`](../../alfred/meeting_adapter.py),
  [`meeting_models.py`](../../alfred/meeting_models.py),
  [`meeting_cleanup.py`](../../alfred/meeting_cleanup.py)): a contract for recording, transcript
  evidence, validated minutes and PDF delivery.

```{important}
**No meeting provider ships in this repository.** `Alfred(config, meeting_adapter=None)` is the
default, so every meeting skill reports *unavailable*. A real recorder must be injected by trusted
host code. Passing the fake-provider tests does **not** show that recording, transcription,
deletion or QR/OBEX delivery works on a Jetson. None of this is hardware-validated.
```

## Skill registry

```bash
alfred --skills
```

Prints a JSON list. No models, microphone or network are used. Each entry has `name`, `category`,
`version` (always `1`), `available`, `reason`, `requires_consent`, `requires_local_approval` and
`evidence_only`.

| Skill | Category | Available when | Consent | Local approval | Evidence only |
|---|---|---|---|---|---|
| `gpu_status` | system | `tegrastats` is on `PATH` | no | no | no |
| `health_graph` | workflow | An **approved** workflow named `health_graph` exists | no | yes | no |
| `reminders` | reminder | The local store and automations objects exist | no | no | no |
| `meeting_recording` | meeting | Provider attests `microphone`, `guarded_ssd`, `recording` | yes | yes | no |
| `transcript_retrieval` | meeting | Provider attests `transcription`, `guarded_ssd` | yes | yes | yes |
| `qwen_minutes` | meeting | Provider attests `transcription`, `qwen`, `safe_pdf` | yes | yes | yes |
| `qr_obex_handoff` | delivery | Provider attests `safe_pdf`, `qr_obex` | yes | yes | no |

When unavailable, `reason` explains why (at most 160 characters), for example
`Offline meeting adapter not installed`.

**A skill is metadata, not a tool.** The registry adds nothing to `TOOL_SCHEMAS`, loads no
plugins, runs no shell and grants no permission. It is not part of the schemas given to Needle,
Qwen or the Harness. Availability is probed from the current environment, never inferred from a
branch name.

## Meeting adapter

`OfflineMeetingAdapter(provider, journal=MeetingCleanupJournal(store))` is a boundary around a
**provider** supplied by the host. The adapter enforces consent, scope, validation and cleanup.
The provider does the device work.

### Provider contract

| Method | Required for | Notes |
|---|---|---|
| `capability(feature) -> bool` | All | Features: `microphone`, `guarded_ssd`, `recording`, `transcription`, `qwen`, `safe_pdf`, `qr_obex`. Anything but `True` counts as unavailable. |
| `start_recording(consent, retention, operation_id) -> meeting_id` | Recording | Returns an opaque ID matching the meeting-ID pattern. |
| `rollback_start(operation_id) -> True` | Recording | Idempotently undo only that start attempt. Mandatory before a microphone is activated. |
| `recording_status(meeting_id) -> {"state": ...}` | Status | State is `recording` or `stopping`. |
| `stop_recording(meeting_id)` | Stop | Must be idempotent. |
| `discard_meeting(meeting_id) -> True` | Non-retained stop | Must attest real deletion. |
| `get_segments(meeting_id)` | Evidence | Current meeting only. Never a history or filesystem search. |
| `suggest_minutes(packet)` | Minutes | Proposal that cites segments. |
| `save_pdf(meeting_id, minutes, segments) -> handle` | Export | Returns an opaque artifact ID. Paths from a model are never accepted. |
| `deliver_pdf(meeting_id, handle) -> {"delivered": bool}` | Delivery | |

### Operations and who may call them

| Adapter method | Needs | Result |
|---|---|---|
| `start(...)` | `local_authorized=True`, `consent=True`, a journal, a provider with `rollback_start` | `{meeting_id, status: "recording", identity: "mic_channel_not_person", retention}` |
| `permit(...)` | `local_authorized=True`, `consent=True`, TTL 1 to 3600 s (default 600) | An immutable `MeetingPermit` bound to one task and one meeting. |
| `status(permit)` | Valid permit, active meeting | Recording state. |
| `stop(permit, local_authorized=True)` | **Separate** local authorization | Stops the recorder. Non-retained meetings are discarded and unreadable at once. |
| `evidence(permit)` | Valid permit | Sanitised segments. |
| `minutes(permit, title=...)` | Valid permit | Validated minutes. |
| `export_pdf(permit, minutes)` | Minutes validated for this task and evidence | `{artifact_id, delivery: "not_sent"}` |
| `deliver(permit, artifact_id, local_authorized=True)` | Separate local approval; artifact belongs to this task | `{meeting_id, delivered}` |
| `revoke(permit)` | The exact permit object | Removes access for this task. |
| `clear_session(session_id, owner)` | none | Revokes permits and artifacts. Does **not** stop recordings. |
| `recover_cleanup(session_id, owner, local_authorized=True)` | Local operator | Retries stop, discard or rollback from the journal, including after a restart. |

A `MeetingPermit` is rejected if it is forged, revoked, expired, for another session or owner, or
for a meeting that is stopping or purged. Every adapter operation runs under one re-entrant lock,
so an in-flight read finishes before a stop or revocation starts. The lock cannot interrupt a
hung native device call; providers must set their own timeouts.

### Evidence rules

`sanitize_segments` accepts 1 to 64 segments and at most 16 000 bytes of JSON. Per segment:

- `source` is a microphone channel identifier. `speaker` is set to that channel,
  `identity_type` is `microphone_channel` and `verified_identity` is `false`. **A channel is not a
  person.**
- Timestamps are normalised to UTC with a `Z` suffix. `end` before `start` is an error.
- `text` is truncated to 260 characters. Control characters are replaced, and `sk-` keys, bearer
  tokens and email addresses are redacted. Redaction is best effort, not a guarantee.
- Overlap links (at most 16) must point to other segments in the packet.
- `synchronization_uncertainty_ms` is 0 to 30 000 or absent.
- Up to 48 word timings are kept. Timing is labelled `decoder_estimated`.

### Minutes validation

Model output is untrusted until `validate_minutes_proposal` accepts it. The proposal must have
exactly `summary`, `agenda`, `key_points`, `decisions`, `action_items`, `risks`,
`open_questions`. Each statement must cite an existing `segment_id` and give a quote of 2 to 300
characters that appears **verbatim** in that segment (case-insensitive).

For action items:

- `owner` must be `Unassigned` or appear in the cited task quote. A microphone channel name cannot
  be an owner.
- `due` must be `Not specified` or an ISO `YYYY-MM-DD` date that appears in the cited task quote.
- Each normalised item records `source_segment_id` and `evidence_quote`.

A PDF can only be written for minutes whose digest **and** evidence snapshot were validated for
the same task. If the transcript changes, the minutes must be validated again.

Quotes can contain prompt injection from the meeting itself. A quote is user content, not
authority. It cannot grant shell, file, network, approval or workflow access.

### Cleanup journal

`MeetingCleanupJournal` stores opaque operation tokens in SQLite so cleanup survives a restart. It
holds no audio or transcript. See [Data model](../architecture/data-model.md).

States: `starting`, `recording`, `stopping`, `purge_pending`, `rollback_pending`,
`stopped_retained`. A unique index on non-null meeting IDs makes a second claim of the same
meeting fail, even from another adapter instance or after a restart. A `stopped_retained` row
keeps the ID reserved but is not a cleanup request.

If a rollback or discard cannot be verified, the row stays and the call raises. Alfred never
reports a successful erase it could not confirm.

Everything else about a meeting (permits, validated minutes, artifact handles) lives in memory only
and is never stored as cognitive facts.

## Boundaries with the rest of Alfred

- Needle and Qwen cannot invoke provider methods. Intents need local authorization and a matching
  per-task permit.
- Nothing in audio, Needle or Qwen starts a recording or searches transcripts automatically.
- The workflow engine is unchanged. A skill does not make a workflow approved or executable.
- `runtime.context_mode: "meeting"` scopes retained facts. It does not start a recording.

## Related

- [Security model](../architecture/security-model.md)
- [Tools](tools.md)
