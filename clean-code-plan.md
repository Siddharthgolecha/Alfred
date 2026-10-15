# Clean Code Sweep — Alfred Repository

## Overview

A full clean-code sweep of the Alfred codebase applying Robert C. Martin's principles.
The audit found 153 issues across 22 source files. All three severity tiers are addressed:
Tier 1 (structural/SRP), Tier 2 (naming/duplication/magic constants), and Tier 3 (comments/dead code/formatting).

**Key decisions:**
- `app.py`'s God class is split into focused modules: `session.py`, `router.py`, `listener.py`
- Refactored classes stay in new files — each responsibility maps to one module
- Every touched module gets corresponding unit tests (F.I.R.S.T.-aligned)
- Changes are applied file-by-file, one sub-task at a time, so diffs stay reviewable

**Files involved:** `alfred/app.py`, `alfred/runtime.py`, `alfred/backends.py`, `alfred/config.py`,
`alfred/automations.py`, `alfred/system2.py`, `alfred/audio.py`, `alfred/meeting_adapter.py`,
`alfred/skills.py`, `alfred/models.py`, `alfred/wakeword.py`, `alfred/meeting_models.py`,
`alfred/workflow_engine.py`, `alfred/lifecycle.py`, `alfred/console.py`,
`alfred/inference_admission.py`, `alfred/release_gates.py`, `alfred/cli.py`,
`alfred/validators.py`, `alfred/backends.py`, `alfred/constants.py`

---

## Sub-Task 1 — Split `app.py` God Class into Focused Modules

**Status:** `[x] completed`

### Intent
`alfred/app.py` hosts a single `Alfred` class with 20+ methods spanning session lifecycle,
request routing, wake-word listening, and Needle/Qwen integration. This violates SRP and
makes the class impossible to test in isolation. Split responsibilities into three new modules,
keeping `app.py` as a thin composition root.

### Expected Outcomes
- `alfred/session.py` — session lifecycle (start, stop, context, state machine)
- `alfred/router.py` — request routing logic (Needle dispatch, Qwen handoff)
- `alfred/listener.py` — wake-word loop and audio input management
- `alfred/app.py` reduced to a composition root that wires the three objects
- No existing public API or entry-point behaviour changes

### Todo List
1. Read `alfred/app.py` in full; map every method to one of: session / router / listener / composition
2. Create `alfred/session.py` with `SessionManager` class; move session methods; add type hints
3. Create `alfred/router.py` with `RequestRouter` class; move routing methods; add type hints
4. Create `alfred/listener.py` with `WakeWordListener` class; move listening loop; add type hints
5. Rewrite `alfred/app.py` as a thin `Alfred` composition root that owns and delegates to the three classes
6. Fix all flag arguments in split methods (F3 smell) — convert to explicit keyword-only params
7. Rename all single-char or abbreviated local variables (`s`, `p`, `m`, `q`) to descriptive names (N1–N7)
8. Replace bare `except` blocks with typed catches; remove silent failures (return `{}`) → raise `ValueError`
9. Update all imports across the codebase to point to the new modules

### Relevant Context
- `alfred/app.py` — God class to decompose
- `alfred/routing.py` — legacy re-export shim; update or remove after split
- `alfred/feedback.py` — referenced by the listener; keep as-is
- Look at `threading.RLock` usage (`_needle_task_lock`) — rename to `_needle_execution_lock` (N1)

---

## Sub-Task 2 — Refactor `runtime.py` Monolithic `Tools.execute()`

**Status:** `[x] done`

### Intent
`Tools.execute()` is a 20+ branch monolith that handles every tool type inline. This violates
SRP and OCP — adding a new tool requires editing the same method. Introduce a **tool-registry
pattern**: each tool type is a small, registered handler function. Repeated SQL patterns are
extracted to private helper methods.

### Expected Outcomes
- `Tools.execute()` becomes a dispatch call: look up handler by tool name, call it
- Each tool handler is a named private method or registered callable (≤20 lines each)
- SQL INSERT/UPDATE patterns deduplicated into `_upsert_reminder()`, `_update_schedule()` helpers
- Abbreviated names (`STR`, `NUM` aliases) removed or given descriptive names
- Inline `max(1, int(float(...) / frame_secs))` calculation extracted to a named constant or helper

### Todo List
1. Read `alfred/runtime.py` in full; list all tool-type branches in `execute()`
2. Extract each branch to a private method named `_handle_<tool_name>()`
3. Build a `_TOOL_HANDLERS: dict[str, Callable]` dispatch table
4. Replace `execute()` body with a single dispatch lookup + call
5. Extract repeated SQL patterns into private helpers (`_upsert_reminder`, `_update_schedule`, etc.)
6. Replace `STR`/`NUM` abbreviations with `_str_schema`/`_num_schema` calls directly
7. Tighten broad `except (OSError, ValueError, RuntimeError, TypeError, KeyError)` to typed catches

### Relevant Context
- `alfred/runtime.py` — primary file; ~435 lines
- `alfred/constants.py` — already centralises constants; move any new named constants here

---

## Sub-Task 3 — Refactor `backends.py` `ModelSupervisor`

**Status:** `[x] done`

### Intent
`ModelSupervisor.ensure_running()` mixes three concerns: health-checking, binary resolution,
and process startup. Port/binary lookup logic is duplicated across functions. Hardcoded sentinel
ports (11434, 1234) need to become named constants.

### Expected Outcomes
- `ensure_running()` delegates to three private methods: `_check_health()`, `_resolve_binary()`, `_start_process()`
- Port/binary resolution logic consolidated into one private helper (no duplication)
- Sentinel port values moved to module-level constants
- `u = urlsplit(...)` renamed to `url_parts`
- Broad exception tuple at lines 118–119 tightened

### Todo List
1. Read `alfred/backends.py` in full
2. Extract health-check logic → `_check_health()`
3. Extract binary resolution → `_resolve_binary()`
4. Extract process startup → `_start_process()`
5. Deduplicate port/binary lookup helper
6. Add module-level constants for sentinel ports
7. Rename `u` → `url_parts` and other single-char identifiers
8. Narrow the broad except clause

### Relevant Context
- `alfred/backends.py` — primary file; ~302 lines
- Port constants may belong in `alfred/constants.py`

---

## Sub-Task 4 — Clean `config.py` `load_config()` Mixed Concerns

**Status:** `[x] done`

### Intent
`load_config()` performs four distinct operations (load, merge, env-var override, validate)
and duplicates validation already present in Pydantic validators. Abbreviated local `q` for the
Qwen config sub-dict is misleading. Named range limits (1, 30, 131072) need constants.

### Expected Outcomes
- `load_config()` delegated to four private functions: `_load_raw()`, `_merge_defaults()`, `_apply_env_overrides()`, `_validate()`
- Validation logic lives in one place (Pydantic models) — removed from `load_config()` where duplicated
- `q` renamed to `qwen_cfg` throughout
- Range bounds moved to named constants in `alfred/constants.py`
- Redundant comments removed

### Todo List
1. Read `alfred/config.py` in full
2. Extract `_load_raw()`, `_merge_defaults()`, `_apply_env_overrides()`, `_validate()` private helpers
3. Remove duplicated validation from `load_config()` (already enforced by Pydantic)
4. Rename `q` → `qwen_cfg`
5. Move range bounds to `alfred/constants.py`
6. Remove redundant inline comments that restate type-hint-obvious behavior

### Relevant Context
- `alfred/config.py` — primary file; ~453 lines
- `alfred/constants.py` — target for new named range constants

---

## Sub-Task 5 — Harden Error Handling Across `system2.py` and `meeting_adapter.py`

**Status:** `[x] done`

### Intent
`system2.py` silently returns `{"steps": []}` when actions are rejected instead of raising,
and has misleading constant names. `meeting_adapter.py` has bare `except Exception:` masks,
a `_revoke_meeting_access()` with three mixed concerns, and deep dict access without validation.

### Expected Outcomes
- Silent `{"steps": []}` returns replaced with `ValueError` raises in `system2.py`
- `VOICE_ALLOWED` / `SENSITIVE` frozensets renamed to `VOICE_EXECUTABLE_TOOLS` / `APPROVAL_REQUIRED_TOOLS`
- `continue_handoff()` 5-arg signature refactored to use a dataclass config
- `_revoke_meeting_access()` split into `_revoke_permits()`, `_delete_minutes()`, `_delete_artifacts()`
- Bare `except Exception:` replaced with typed catches in `meeting_adapter.py`
- `_verify()` param `p` → `permit`

### Todo List
1. Read `alfred/system2.py` in full
2. Replace silent `{"steps": []}` returns with `ValueError`; update callers
3. Rename `VOICE_ALLOWED` → `VOICE_EXECUTABLE_TOOLS`, `SENSITIVE` → `APPROVAL_REQUIRED_TOOLS`; update references
4. Create `HandoffConfig` dataclass; refactor `continue_handoff()` signature
5. Extract duplicated permission-error patterns to a private helper
6. Read `alfred/meeting_adapter.py` in full
7. Split `_revoke_meeting_access()` into three focused private methods
8. Replace `except Exception:` with typed catches
9. Rename `p` → `permit` throughout
10. Extract deep dict access to a helper that validates structure

### Relevant Context
- `alfred/system2.py` — ~422 lines; caller of `continue_handoff()` lives in `app.py` (post-split: `router.py`)
- `alfred/meeting_adapter.py` — ~407 lines

---

## Sub-Task 6 — Fix Naming, Magic Constants, and Duplication in `audio.py`, `automations.py`, and `workflow_engine.py`

**Status:** `[x] done`

### Intent
These three files share the same category of issues: abbreviated names, magic numbers,
duplicated SQL/state-transition patterns, and flag arguments. Group them into one sub-task
because changes are contained within each file (no cross-file refactoring).

### Expected Outcomes
**`audio.py`**
- `_descrip()` → `_format_device_description()`; `explicitly_selected` flag argument removed (split into two calls)
- `4294967295` and other magic sentinel values moved to module-level named constants
- Train-wreck chain on optional bluetooth address extracted to a safe helper
- Duplicated port conversion logic extracted to `_to_port_list()`

**`automations.py`**
- `hh, mm` → `hour, minute`
- Threshold bounds `20<=threshold_c<=110` extracted to constants `MIN_CELSIUS`, `MAX_CELSIUS`
- `tick()` split into `_check_reminder_jobs()`, `_check_watch_conditions()`, `_deliver_notifications()`
- Repeated UPDATE SQL patterns extracted to `_mark_fired()` helper

**`workflow_engine.py`**
- `_ID` regex renamed to `_STEP_ID_PATTERN`
- `validate_graph()` split: structure validation / name validation / service-allowlist validation
- `MAX_STEPS`, `MAX_RUNS_PER_TICK` documented with a comment explaining rationale
- Redundant docstrings that restate type hints removed

### Todo List
1. Read `alfred/audio.py`, `alfred/automations.py`, `alfred/workflow_engine.py` in full
2. Apply naming fixes per module
3. Extract magic constants to module-level names (or `alfred/constants.py` if shared)
4. Split flag-argument functions into two explicit functions
5. Deduplicate SQL/state-transition patterns
6. Split multi-concern functions

### Relevant Context
- `alfred/constants.py` — check before adding new constants to avoid duplication

---

## Sub-Task 7 — Clean Up `skills.py`, `models.py`, `wakeword.py`, `inference_admission.py`, `lifecycle.py`, `console.py`, `cli.py`

**Status:** `[x] done`

### Intent
Smaller files with isolated issues: single-char identifiers, hardcoded literals, over-broad
exception handling, multi-concern functions, and misleading comments. Addressed here as a
batch to avoid one sub-task per small file.

### Expected Outcomes
- `skills.py`: `m` → `meeting`; `list()` split into `_gpu_available()`, `_health_ok()`, `_meeting_available()` helpers; typed exception catch
- `models.py`: `max_new_tokens=384` extracted to `MAX_NEW_TOKENS` constant; deep dict access guarded
- `wakeword.py`: `2000` ms → `MIN_UTTERANCE_MS` constant; `pieces` → `confirmed_text`
- `meeting_models.py`: `source` variable name disambiguated; duplicated `_clock_time()` call reduced
- `inference_admission.py`: `HandoffConfig` or similar; `rem` → `remaining_seconds`; double-wrapped exception resolved
- `lifecycle.py`: `sock` → `listener_socket`; `except BaseException:` narrowed; race-condition comment → extracted method
- `console.py`: `_ANSI`, `_CONTROL` → `_ANSI_ESCAPE_PATTERN`, `_CONTROL_CHAR_PATTERN`; escape sequences → named constants; complex re-render logic documented
- `cli.py`: `s` → `supervisor`; `doctor()` split into `_diagnose_audio()`, `_diagnose_qwen()`, `_diagnose_harness()`

### Todo List
1. Read each file listed above
2. Apply naming, constant-extraction, and exception-handling fixes
3. Split multi-concern functions
4. Remove redundant/misleading comments

### Relevant Context
- `alfred/constants.py` — shared home for new named constants
- `alfred/validators.py` — fix misleading `TypeVar` docstring

---

## Sub-Task 8 — Remove Dead Code and Clean Comments Across All Files

**Status:** `[x] done`

### Intent
Remove commented-out code blocks (violates C1 — version control already preserves history),
unused imports, and unreachable branches. This is a global sweep of all files touched by
previous sub-tasks.

### Expected Outcomes
- All commented-out code blocks deleted
- All unused imports removed (confirmed with a `grep` pass)
- All unreachable conditions removed (e.g. `if field == "summary"` that is always true for first iteration in `meeting_models.py`)
- Redundant docstrings that restate type hints removed

### Todo List
1. Grep for commented-out code patterns (`# .*=`, `# [a-zA-Z_].*\(`) across `alfred/`
2. Review each hit; delete confirmed dead comment blocks
3. Run `ruff check --select F401` (or equivalent) to surface unused imports; remove them
4. Verify no remaining `TODO`/`FIXME` comments without associated issue references

### Relevant Context
- All files touched by Sub-Tasks 1–7

---

## Sub-Task 9 — Write and Update Unit Tests for All Refactored Modules

**Status:** `[x] done`

### Intent
Apply F.I.R.S.T. principles (Fast, Independent, Repeatable, Self-Validating, Timely) to every
module touched by the refactor. Each test file covers exactly one module and has one concept
per test function.

### Expected Outcomes
- `tests/test_session.py` — covers `SessionManager` (new)
- `tests/test_router.py` — covers `RequestRouter` (new)
- `tests/test_listener.py` — covers `WakeWordListener` (new)
- `tests/test_runtime.py` — updated; one test per `_handle_<tool_name>()` handler
- `tests/test_backends.py` — updated; covers `_check_health`, `_resolve_binary`, `_start_process`
- `tests/test_config.py` — updated; covers each of the four private `load_config` helpers
- `tests/test_system2.py` — updated; tests that rejected actions raise `ValueError`
- `tests/test_automations.py` — updated; tests `_check_reminder_jobs`, `_check_watch_conditions`, `_deliver_notifications` independently
- All tests pass; no new warnings in CI

### Todo List
1. Locate existing test directory and existing test files
2. For each new module (`session.py`, `router.py`, `listener.py`), create matching `test_<module>.py`
3. For each modified module, review existing tests and add missing cases
4. Ensure every test is independent (no shared mutable state), fast (no real I/O without mocking), and self-validating (assert, no print)
5. Run the full test suite and confirm green

### Relevant Context
- Existing tests live in `tests/`; follow their conventions for fixtures and mocking
- Use `unittest.mock` or `pytest-mock` consistent with the existing test suite

---

## Implementation Order

```
Sub-Task 1 (app.py split)
    → Sub-Task 2 (runtime.py)
    → Sub-Task 3 (backends.py)
    → Sub-Task 4 (config.py)
    → Sub-Task 5 (system2 + meeting_adapter)
    → Sub-Task 6 (audio + automations + workflow_engine)
    → Sub-Task 7 (small files batch)
    → Sub-Task 8 (dead code sweep)
    → Sub-Task 9 (tests)
```

Sub-Tasks 2–4 are independent of each other and can proceed after Sub-Task 1 completes.
Sub-Tasks 5–7 are also independent of each other.
Sub-Task 8 depends on all previous sub-tasks being complete.
Sub-Task 9 depends on Sub-Tasks 1–8.
