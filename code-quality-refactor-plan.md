# Alfred Codebase Quality Refactor Plan

## Overview

This plan covers a full quality pass of the Alfred codebase, addressing structural issues, Pydantic v2 adoption, magic-number extraction, type-hint coverage, file splitting, and test updates. The goal is a significantly cleaner, more maintainable codebase with no behaviour changes.

**Scope**: All files under `alfred/` and `alfred/cognitive/`. No external API or protocol changes.  
**Pydantic target**: Pydantic v2 (`pydantic>=2`) and `pydantic-settings>=2`.  
**Type hints**: Added inline as each file is touched, not as a separate final pass.  
**Non-goals**: Performance optimisation, adding new features, changing threading model.  
**Branch**: `maestaris/refactor-code-quality` (created from latest `main`).

---

## Sub-Tasks

---

### Sub-Task 1 — Create `alfred/constants.py` and extract all magic numbers

**Status**: [x] done

**Intent**  
All hardcoded numeric limits, string lengths, timeouts, TTL values, and regex patterns are scattered across 15+ files, making them impossible to reason about or change safely. Centralising them into a single constants module removes duplication and makes limits discoverable.

**Expected Outcomes**
- A new `alfred/constants.py` with named sections: `Truncation`, `Limits`, `Timeouts`, `TTL`, `Patterns`.
- Every file that previously used a raw literal (e.g. `[:300]`, `timeout=4`, `MAX_FACTS = 8`) now imports from `constants`.
- No functional behaviour changes — same numeric values, just named.
- Full type annotations on all constants.

**Todo List**
1. Create `alfred/constants.py` with grouped module-level namespaces (simple classes with class-level attributes, or plain module constants with clear naming sections).
2. Audit every `.py` file for magic numbers using `grep` on patterns: `[:`, `timeout=`, `ttl`, `maxLength`, `MAX_`, hardcoded regex strings.
3. Replace each occurrence with the matching constant import.
4. Add type hints to the constants file itself.
5. Run existing tests to confirm nothing changed.

**Relevant Context**
- Key sites: `app.py` (lines 50, 271, 278, 494, 513, 617, 668), `runtime.py` (lines 23–24, 85, 307, 352), `system2.py` (lines 59, 163, 180), `cognitive/context.py` (lines 40–41, 92–93), `cognitive/contracts.py` (lines 15, 92, 185), `models.py` (line 37), `meeting_adapter.py` (lines 29–30, 93, 145), `cognitive/harness_sdk.py` (lines 172, 190–192).
- All regex patterns serving as validators (model name, scope ID, fact key) belong in `constants.Patterns`.

---

### Sub-Task 2 — Introduce `alfred/validators.py` (shared validation helpers)

**Status**: [x] done

**Intent**  
The same validation logic (TTL range check, type assertion, compact JSON serialisation, string truncation) is re-implemented independently in 8+ files. A shared `validators.py` module provides tested, reusable helpers and eliminates all `type(x) is not T` anti-patterns.

**Expected Outcomes**
- New `alfred/validators.py` with fully typed helpers: `validate_ttl_seconds()`, `validate_type()`, `truncate()`, `to_compact_json()`, `validate_string_field()`.
- All existing call sites replaced with imports from `validators`.
- Duplicate `json.dumps(sort_keys=True, separators=(",",":"), allow_nan=False)` removed from every location.
- Unit tests added for every helper in `validators.py`.

**Relevant Context**
- TTL validation pattern repeated in: `config.py`, `cognitive/context.py` (line 92), `cognitive/needle_loop.py` (line 87).
- `to_compact_json` pattern repeated 5+ times across `runtime.py`, `cognitive/contracts.py`, `models.py`.
- `truncate()` calls (`[:300]`, `[:1200]`, etc.) appear 10+ times — should accept a `Truncation` constant as the length argument.

---

### Sub-Task 3 — Adopt Pydantic v2 for configuration (`alfred/config.py` + `pydantic-settings`)

**Status**: [x] done

**Intent**  
The hardcoded `DEFAULT` dict and manual recursive merge in `config.py` provide no type safety and scatter default values across multiple files. Replacing with a `pydantic-settings` `BaseSettings` model gives typed attribute access, automatic env-var override, and centralised defaults — eliminating all raw dict access patterns like `cfg["runtime"]["database"]`.

**Expected Outcomes**
- `alfred/config.py` re-implemented using `pydantic-settings.BaseSettings` (v2) with nested Pydantic v2 models for each config section: `RuntimeConfig`, `AudioConfig`, `ModelConfig`, `HarnessConfig`, etc.
- All callers updated to use typed attribute access (`cfg.runtime.database`) instead of dict access.
- `release_gates.py` merged into `config.py` (it only references config values and version constants).
- Environment variable override works natively via `pydantic-settings`.
- `pydantic-settings>=2` added to `pyproject.toml` if not already present.
- Existing config tests updated; new tests for field defaults, required fields, and env-var override.

**Relevant Context**
- `alfred/config.py` (lines 1–170): `DEFAULT` dict, `_merge()`, `load()`, `validate()`.
- `alfred/release_gates.py`: Only references config values and version constants — safe to absorb.
- All callers doing `cfg["key"]["subkey"]` — locate with `grep "cfg\["` and update all.

---

### Sub-Task 4 — Adopt Pydantic v2 for `alfred/cognitive/contracts.py`

**Status**: [x] done

**Intent**  
`contracts.py` uses `@dataclass(frozen=True)` with manual `validate()` methods, custom JSON hooks, and hardcoded byte limits. Pydantic v2 `BaseModel` with `model_validator` and `field_validator` replaces all of that with cleaner, tested, self-documenting validation.

**Expected Outcomes**
- All contract classes inherit from `pydantic.BaseModel` with `model_config = ConfigDict(frozen=True)`.
- Manual `validate()` methods replaced by `@model_validator(mode="after")` / `@field_validator`.
- Custom `_unique_pairs()` JSON hook removed; duplicate-key rejection handled via Pydantic or an explicit validator.
- All byte/character limits imported from `alfred/constants.py` (Sub-Task 1).
- Full type annotations on all fields.
- All callers updated; existing tests updated.

**Relevant Context**
- `alfred/cognitive/contracts.py` (lines 1–200+): `Contract` base class + 8 subclasses.
- `_unique_pairs()` (line 30): custom `object_pairs_hook` — can be replaced with a Pydantic root validator that checks for duplicate keys after JSON parse.
- `harness_bridge.py` and `needle_loop.py` both import from `contracts.py` — update their import side-effects.

---

### Sub-Task 5 — Adopt Pydantic v2 for `alfred/cognitive/context.py` slots and facts

**Status**: [x] done

**Intent**  
The `Slot` dataclass uses manual `__post_init__` checks and raw dict storage. Replacing it with a Pydantic v2 model adds automatic field validation, explicit schema, and removes the fragile manual checks in `_validate_fact()` and `valid_id()`.

**Expected Outcomes**
- `Slot` converted to `pydantic.BaseModel` with v2 `@field_validator` for `ttl`, `budget_chars`, and `key` format.
- `_validate_fact()` and `valid_id()` replaced by a `Fact` Pydantic model with inline validators.
- Hardcoded limits in `Slot` imported from `alfred/constants.py`.
- Full type annotations added to `ContextManager` methods.
- Context construction unit tests updated.

**Relevant Context**
- `alfred/cognitive/context.py` (lines 72–85): `Slot` dataclass.
- (Lines 25–41): module-level constants and regex → already moved to `constants.py` in Sub-Task 1.
- `ContextManager._slots` (`dict[str, Slot]`) and `._facts` (`dict[str, str]`) shapes must be preserved for callers.

---

### Sub-Task 6 — Split `alfred/cognitive/harness_sdk.py` into three files

**Status**: [x] done

**Intent**  
`harness_sdk.py` (490+ lines) mixes SDK wrapper logic, configuration validation, and audit checks in a single file. Splitting into three single-responsibility files makes each concern independently testable and navigable.

**Expected Outcomes**
- `alfred/cognitive/harness_sdk.py` — retains only `LocalHarness` wrapper class and its direct runtime dependencies.
- `alfred/cognitive/harness_audit.py` — contains `audit_effective_cordis_config()` and all audit SQL/regex logic.
- `alfred/cognitive/harness_config.py` — config constants, model-name regex, timeout range, `DENIED_ROWS` frozenset; imports limits from `alfred/constants.py`.
- `SDK_VERSION` constant moved to `alfred/constants.py`.
- Full type annotations added to all three files.
- All import sites updated (`cognitive/__init__.py`, `backends.py`, `orchestrator.py`).

**Relevant Context**
- `alfred/cognitive/harness_sdk.py` (lines 36–46): `SDK_VERSION`, `DENIED_ROWS` → `harness_config.py` / `constants.py`.
- (Lines 151–180): audit SQL checks → `harness_audit.py`.
- (Lines 172, 190–192, 274): repeated regex patterns → `alfred/constants.py`.

---

### Sub-Task 7 — Split `alfred/app.py` into focused modules

**Status**: [ ] pending

**Intent**  
`app.py` is 1,110 lines with a single `Alfred` class handling orchestration, voice capture, escalation, approval, and diagnostics. Splitting into focused files makes each concern independently testable, reduces merge conflicts, and makes the code far easier to navigate.

**Expected Outcomes**
- `alfred/orchestrator.py` — `Alfred` class with `__init__`, service startup, `reason()`, `_process_authorized_task()`, and routing helpers (absorbed from `routing.py`). Full type hints.
- `alfred/voice_loop.py` — `listen()`, audio capture coordination, wakeword integration. Full type hints.
- `alfred/handoff.py` — `_handoff()`, `_observe_needle_prefix()`, `_finish_handoff()`, escalation state machine. Full type hints.
- `alfred/diagnostics.py` — `doctor()` and all health-check helpers. Full type hints.
- `alfred/app.py` — CLI entry only: `Feedback` class, `_main()` argument parsing, `main()` entry point; imports from the four modules above.
- `alfred/routing.py` deleted (content absorbed into `orchestrator.py`).
- All import sites updated.

**Relevant Context**
- `alfred/app.py` (lines 37–55): `Feedback` → stays in `app.py`.
- (Lines 56–232): `Alfred.__init__`, service startup, `reason()` → `orchestrator.py`.
- (Lines 188–279): handoff methods → `handoff.py`.
- (Lines 316–625): `_process_authorized_task` → `orchestrator.py`.
- (Lines 626–699): `listen()` → `voice_loop.py`.
- (Lines 742–810): `doctor()` → `diagnostics.py`.
- (Lines 811–1089): `_main()` → stays in `app.py`.
- `alfred/routing.py` (65 lines) — single consumer is `app.py` / orchestrator; merge and delete file.

---

### Sub-Task 8 — Split `alfred/meeting_adapter.py` into model and adapter files

**Status**: [x] done

**Intent**  
`meeting_adapter.py` (600+ lines) mixes Pydantic/dataclass models, segment validators, and the main adapter class. Separating models from the adapter follows the same single-responsibility principle as Sub-Tasks 6 and 7.

**Expected Outcomes**
- `alfred/meeting_models.py` — Pydantic v2 models for `Segment`, `GrantConsent`, and related types; `sanitize_segments()` validator. Full type hints.
- `alfred/meeting_adapter.py` — retains only `OfflineMeetingAdapter` class, importing from `meeting_models.py`. Full type hints.
- All hardcoded limits (`MAX_SEGMENTS`, `MAX_EVIDENCE_BYTES`) imported from `alfred/constants.py`.
- Existing tests updated; new tests for Pydantic model validation.

**Relevant Context**
- `alfred/meeting_adapter.py` (lines 25–35): regex → `constants.py`.
- (Lines 29–30): `MAX_SEGMENTS`, `MAX_EVIDENCE_BYTES` → `constants.py`.
- (Lines 208–215): `GrantConsent` dataclass → Pydantic v2 model in `meeting_models.py`.
- Import sites: `runtime.py`, `orchestrator.py`.

---

### Sub-Task 9 — General code quality, readability, and technical debt reduction

**Status**: [x] done

**Intent**  
Beyond structural splits and Pydantic adoption, the codebase has accumulated technical debt across the remaining files that were not fully addressed in earlier sub-tasks: broad `except Exception` clauses that swallow errors, missing docstrings on all public methods, SQLite thread-safety issues, complex nested conditionals that could be flattened, dead code, and inconsistent naming conventions. This sub-task addresses all of that in a single focused pass.

**Expected Outcomes**
- All `except Exception` catches narrowed to specific exception types, or re-raised with context (`raise ... from exc`).
- Public methods in all files have concise one-line docstrings (or full docstrings for complex logic).
- SQLite connections in `runtime.py` and `cognitive/sessions.py` use proper thread-safe patterns (connection-per-call or an explicit lock; remove `check_same_thread=False` where possible).
- Deeply nested `if/else` conditionals in `system2.py` (`execute_or_stage()`), `cognitive/needle_loop.py`, and `orchestrator.py` (`_process_authorized_task`) refactored using early-return / guard-clause patterns.
- Dead or unreachable code removed.
- Naming conventions made consistent across all modules (snake_case for functions, UPPER_CASE for module-level constants, PascalCase for classes).
- `lambda` definitions at module scope in `runtime.py` (lines 23–24) replaced with named functions.
- `workflow_engine.py` receives the same inline treatment: type hints, docstrings, constants from `alfred/constants.py`, and any obvious structural improvements without a full split.
- `audio.py` and `cognitive/sessions.py` receive the same inline treatment.

**Relevant Context**
- `app.py` line 693: `except Exception` in `listen()` loop.
- `runtime.py` lines 23–24: lambda at module scope; lines 100–115: check for SQL injection risk.
- `system2.py` line 165: `execute_or_stage()` nested conditionals.
- `cognitive/needle_loop.py`: nested conditionals in `_bounded_metrics()`.
- `workflow_engine.py` (490+ lines): unreviewed; apply inline quality pass (no split required).
- `audio.py` (350+ lines): missing type hints, mixed validation patterns.
- `cognitive/sessions.py` (330+ lines): partial type hints, mixed validation.

---

### Sub-Task 10 — Comprehensive static analysis pass (`mypy`) and final test suite update

**Status**: [ ] pending

**Intent**  
With all code changes complete, run `mypy --strict` across the entire `alfred/` package to catch any remaining annotation gaps introduced during the refactor, and ensure the full test suite passes cleanly on the refactored codebase.

**Expected Outcomes**
- `mypy --strict alfred/` runs with zero new errors (errors from third-party stubs are acceptable with `# type: ignore` if stubs are unavailable).
- All existing tests pass with updated import paths.
- New unit tests exist for:
  - `alfred/validators.py` helpers (truncate, TTL, compact JSON).
  - Pydantic models in `config.py`, `contracts.py`, `context.py`, `meeting_models.py` (valid construction, rejection of invalid values, defaults, env-var override).
  - `alfred/constants.py` smoke test (all constants importable).
- Test coverage does not decrease from baseline.

**Relevant Context**
- Locate existing tests with `glob tests/**/*.py` before making changes.
- Pydantic v2 model tests should use `pytest` with `pytest-raises` for validation errors (`pydantic.ValidationError`).
- `validators.py` tests are pure unit tests — no mocking or external dependencies.
- Python version target: confirm from `pyproject.toml` to use correct union syntax (`T | None` vs `Optional[T]`).

---

## Implementation Notes

- Branch: `maestaris/refactor-code-quality`, created from latest `main`.
- **Wave 1** (foundation, sequential): Sub-Tasks 1 → 2. Everything else depends on these.
- **Wave 2** (parallel after Wave 1): Sub-Tasks 3–9 can largely proceed independently but should be reviewed one at a time.
- **Type hints** are added inline as each file is touched — not deferred to a separate pass.
- **Sub-Task 10** is the final gate — run only after all other sub-tasks are complete and merged on the branch.
- After each sub-task, update the status in this plan file from `[ ] pending` to `[x] done`.
