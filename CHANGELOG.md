# Changelog

All notable changes to Alfred are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Alfred has not published a release. `pyproject.toml` declares version `0.1.0`, and the git
repository has no tags. The `0.1.0` section below is reconstructed from the commit history
(2026-10-14 to 2026-10-16) and groups the work into the milestones the project called phases.
Numbers such as `#27` refer to GitHub pull requests and issues.

## [Unreleased]

### Added

- Sphinx documentation in `docs/`, organised by topic: getting started, guides, architecture
  with diagrams and decision records, reference, and development. Built with MyST, Furo and
  Mermaid.
- `CHANGELOG.md`, `CONTRIBUTING.md` and `SECURITY.md`.
- `alfred/exceptions.py` with the `AlfredError` base class.
- `alfred/constants.py` and `alfred/validators.py`, which replace repeated magic values and
  inline validation patterns.
- Pydantic v2 models for configuration (`AudioConfig`, `WakeWordConfig`, `QwenConfig`,
  `RuntimeConfig`, `AlfredConfig`), built by `load_config_model()`.
- `docs` extra in `pyproject.toml`.

### Changed

- Split large modules by responsibility: `app.py` into `app.py`, `cli.py`, `feedback.py`,
  `listener.py`, `router.py` and `session.py`; `harness_sdk.py` into `harness_config.py`,
  `harness_audit.py` and `harness_sdk.py`; `meeting_adapter.py` into `meeting_models.py` and
  `meeting_adapter.py`.
- Migrated cognitive contracts and context slots to Pydantic v2 models.
- Narrowed exception handling, added docstrings and type hints, and renamed for clarity across
  `alfred/` (clean-code pass).
- The README is now a short landing page that links into the documentation.

### Documentation

- Replaced the per-phase documents (`PHASE2.md` to `PHASE5_*.md`, `WAKE_WORD.md`,
  `deepseek-harness.md`, `HARNESS_PR27_REVIEW.md`) with the topic-based set above.

### Known issues

- Pydantic-only configuration ranges (for example `audio.frame_ms` and
  `runtime.approval_ttl_seconds`) are checked by `load_config_model()` but not by the
  `load_config()` call that the CLI uses. See
  [ADR 0005](docs/architecture/decisions/0005-validation-layers.md).
- `runtime.voice_alerts` is accepted but not read by any module.
- `config.example.json` differs from the built-in defaults (`qwen.model`, `qwen.max_tokens`).
- Three `ModelTests` in `tests/test_alfred.py` error with
  `module 'alfred.models' has no attribute 'shutil'`. They patch `alfred.models.shutil`, which
  `models.py` no longer imports.
- Nothing has been validated on Jetson hardware.

## [0.1.0] - Unreleased

### Foundation (#1, 2026-10-14)

- Voice pipeline: microphone, Cactus Whistle speech-to-text, Cactus Needle 3 routing, and
  validated Python tools.
- Sony WI-C100 Bluetooth headset support through HFP/HSP, with `auto`, `bluetooth` and `direct`
  audio modes and capture through `parec`.
- Strict allowlists for `systemctl --user` services and named tasks. No arbitrary shell.
- SQLite store for reminders, schedules and events.
- Local Qwen service supervision.

### Microphone fail-fast (#3)

- Reject phantom "Built-in Audio" inputs and monitor sources.
- Require live PCM from the chosen source before loading models. Voice mode exits with status 2
  when no microphone works.
- `--doctor` reports each source as `[CANDIDATE]` or `[REJECTED]`.

### Phase 2: planning and approvals (#4)

- Qwen planning against live tool schemas with a JSON-schema response format.
- Shared preflight validation and a local approval queue: `--pending`, `--approve`, `--reject`
  and `--reason`. Voice cannot approve.
- Needle confidence floor.

### Phase 3: automations (#5)

- Absolute and daily reminders with IANA time zones, and GPU temperature watches with
  hysteresis and cooldown.
- Notification outbox with at-least-once delivery.
- Headless `--scheduler-only` mode and the `alfred-scheduler` service unit.
- Typing-style single-line Whistle transcript in a terminal.

### Phase 4: durable workflow graphs (#7)

- Approved JSON workflow DAGs with 1 to 12 steps, conditions, bounded retries for read-only
  tools, cooperative cancellation and crash-safe state.
- Recurring triggers for read-only graphs.
- `--workflow-*` commands.

### Inference backends (#8)

- `llama serve`, `llama-server`, Ollama and LM Studio, with adoption of a compatible running
  server and managed startup otherwise.
- Backend aliases and per-provider default URLs.

### Wake word and shutdown (#9, #10)

- Wake phrase gating before Needle, Qwen or any tool. Optional openWakeWord acoustic detector
  with a user-supplied ONNX model.
- Graceful shutdown on Ctrl+C and SIGTERM. Only Alfred-owned inference processes are stopped.
- Private Unix-socket `alfred close` and a wake-authorized spoken close.
- Tracking and cancellation of short-lived tool and speech subprocesses.

### Phase 5: cognitive layer and Maestaris

- CI sync of `main` into `intrinsic_jetson` with merge, test and push safeguards, and a single
  deduplicated blocker issue (#20, #22, #35).
- Task, session and event contracts with a SQLite ledger (#12, #25).
- Pinned, offline DeepSeek Harness SDK bridge (#13, #27).
- Bounded Needle feedback loop and trusted context (#14, #28).
- Bounded versioned Needle-to-Qwen handoff with no-replay protection and session resume
  (#15, #32).
- Scoped shared context with explicit retention (#16, #33).
- Non-executable skills registry and an optional consent-based meeting adapter (#17, #36).
- Opt-in inference admission and release gates for the 8 GB Jetson (#18, #38).
- Recovery from malformed Qwen plans (#34).

### Phase 5.1: Harness required (#58, #59)

- The pinned DeepSeek Harness is now a required dependency. Startup fails closed on a missing
  package, wrong version, overlay change or plugin mismatch.
- The `--doctor` command launches the audited Cordis child.
- Admission guards Cordis and Qwen cold starts.
- Opt-in live-model probe `tests/sdk_live_qwen_probe.py`.
- Removed the obsolete `runtime.harness_enabled` flag.

### Not done

- Cordis exposes no model-facing tools yet. Needle's bounded loop still runs in Alfred's own
  process (#58 follow-ups).
- No Jetson hardware validation and no release qualification (#18, #29).
