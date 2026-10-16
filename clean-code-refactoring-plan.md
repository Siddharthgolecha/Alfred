# Clean Code Refactoring Plan for Alfred

## Top-Level Overview

This plan defines a full-stack Clean Code refactoring overhaul for the Alfred codebase guided by Robert C. Martin's Clean Code principles (Single Responsibility Principle, Stepdown Rule for functions, Command Query Separation, Tell Don't Ask, Dependency Inversion Principle, DRY, and explicit error handling). 

**Development Phase Context**: The project is in active development; legacy backward-compatibility shims, deprecated property aliases, and legacy facade adapters are explicitly **not required**. We design direct, clean, and cohesive interfaces across all modules and update internal callers and tests directly.

The overhaul achieves:
1. **Modular Tool Architecture (`alfred/tools/`)**: Tools organized into domain modules (`system.py`, `reminders.py`, `automations.py`, `schedules.py`, `workflows.py`, `reasoner.py`) registered via a `@tool` decorator and managed by a dynamic `ToolRegistry`.
2. **Decoupled STT & System 1 Routing Engines**: Clean `STTEngineProtocol` / `WhistleSTTEngine` (for speech-to-text) and `System1RouterProtocol` / `Needle3Engine` (for fast-path reactive routing) eliminating the hybrid `Cactus` class.
3. **Decomposed RequestRouter**: Eliminates monolithic methods and reduces cyclomatic complexity via single-responsibility helper methods and typed outcome enums.
4. **Clean Store Architecture**: Decomposes multi-concern `Store` into cohesive domain stores (reminders, schedules, automations, workflows, event ledger).
5. **Modernized Composition Root (`Alfred`)**: Removes 30+ property delegation boilerplate methods in favor of clean direct service access and isolated lifecycle/watchdog managers.
6. **Decoupled System 2 & Qwen Planning**: Isolates prompt templates, plan parsing, and approval persistence into dedicated classes.
7. **Robust Error & Type Hierarchy**: Standardizes error codes, removes bare exception handlers, and introduces domain dataclasses and complete type hints.

---

## Sub-Tasks

### Sub-Task 1: RequestRouter Monolith Decomposition
- **Intent**: Refactor `RequestRouter` (specifically `_process_authorized_task`) into cohesive, small, single-responsibility methods following the Stepdown Rule, separating request context resolution, needle loop execution, routing adjustment, and escalation handoff logic.
- **Expected Outcomes**:
  - Cyclomatic complexity of routing methods reduced from >15 to <=8.
  - No monolithic methods exceeding 50 lines.
  - Explicit enum/constants for loop outcomes and decision routes (eliminating magic strings).
  - Clean error propagation and telemetry logging.
  - All router unit tests updated and passing cleanly.
- **Todo List**:
  1. Extract route context resolution and minutes workflow initialization into dedicated helper methods.
  2. Extract response validation and repeated-call detection into a `_validate_needle_calls` helper.
  3. Extract needle loop execution and outcome branching into modular handler functions.
  4. Replace magic string outcomes (`uncertain`, `repeated`, `partial`, `budget`) with typed enums/constants.
  5. Update and run router unit tests in `tests/test_router.py`, `tests/test_voice_routing.py`, and `tests/test_escalation.py`.
- **Relevant Context**:
  - `alfred/router.py`: `RequestRouter._process_authorized_task()`, `adjust_status_calls()`
  - `alfred/cognitive/contracts.py`: Decision and completion structures
  - `tests/test_router.py`, `tests/test_voice_routing.py`, `tests/test_escalation.py`
- **Status**: [x] done

---

### Sub-Task 2: Decouple Needle 3 Router & Whistle STT into Swappable Engines
- **Intent**: Replace the coupled `Cactus` hybrid with distinct, swappable engine abstractions: `STTEngineProtocol` / `WhistleSTTEngine` for streaming audio transcription, and `System1RouterProtocol` / `Needle3Engine` for reactive tool routing and multi-turn feedback.
- **Expected Outcomes**:
  - `alfred/engines/stt.py` (or `alfred/stt.py`): Clean `STTEngineProtocol` and `WhistleSTTEngine` implementation encapsulating streaming frames, keyword handling, partial deduplication, and `admission.claim("whistle")`.
  - `alfred/engines/router.py` (or `alfred/system1.py`): Clean `System1RouterProtocol` and `Needle3Engine` implementation encapsulating `route(transcript)`, `route_feedback(observation_json)`, and `admission.claim("needle")`.
  - Callers in `alfred/app.py`, `alfred/router.py`, and `alfred/listener.py` directly receive injected STT and Router engines.
  - Legacy `Cactus` class removed or replaced with direct engine usage across all tests.
  - All tests in `tests/test_needle_loop.py`, `tests/test_inference_admission.py`, `tests/test_voice_routing.py`, and `tests/test_audio_failfast.py` updated and passing.
- **Todo List**:
  1. Create `STTEngineProtocol` and implement `WhistleSTTEngine`.
  2. Create `System1RouterProtocol` and implement `Needle3Engine`.
  3. Update `Alfred`, `RequestRouter`, and `audio.py` / `listener.py` to directly instantiate and use the decoupled engines.
  4. Update test mocks and fixtures across test files to directly mock the decoupled engine interfaces.
  5. Run engine and router test suites.
- **Relevant Context**:
  - `alfred/models.py`
  - `alfred/audio.py`, `alfred/listener.py`, `alfred/console.py`
  - `alfred/cognitive/needle_loop.py`: `NeedleAgentLoop`
  - `alfred/router.py`: `RequestRouter`
  - `tests/test_needle_loop.py`, `tests/test_inference_admission.py`
- **Status**: [x] done

---

### Sub-Task 3: Modular Tool Architecture & Dynamic Registry with Store Decomposition
- **Intent**: Extract all tool definitions and handlers from `runtime.py` into a modular `alfred/tools/` package using a clean `@tool` decorator and `ToolRegistry`, and decompose the multi-concern `Store` class into focused domain stores.
- **Expected Outcomes**:
  - `alfred/tools/base.py` & `registry.py`: Defines `@tool` decorator, `ToolDefinition`, schema builder, and dynamic tool catalog.
  - Domain tool modules under `alfred/tools/`:
    - `system.py`: `get_system_status`, `get_gpu_status`, `list_services`, `control_service`, `run_named_task`
    - `reminders.py`: `create_reminder`, `create_reminder_at`, `create_daily_reminder`, `list_reminders`, `cancel_reminder`
    - `automations.py`: `watch_gpu_temperature`, `list_automations`, `pause_automation`, `resume_automation`, `cancel_automation`
    - `schedules.py`: `schedule_every`, `list_schedules`, `cancel_schedule`
    - `workflows.py`: `run_workflow`, `list_workflows`
    - `reasoner.py`: `delegate_to_reasoner`
  - `ToolRegistry` provides direct schema enumeration (`get_schemas()`) and validated tool execution (`execute(name, args)`).
  - `Store` decomposed into cohesive domain store managers (reminders, schedules, automations, workflows, events).
  - All callers in `app.py`, `router.py`, `system2.py`, and test files updated to use the new `ToolRegistry`.
  - `tests/test_runtime.py`, `tests/test_automations.py`, and `tests/test_alfred.py` updated and passing.
- **Todo List**:
  1. Create `alfred/tools/base.py` and `alfred/tools/registry.py` with `@tool` registration and execution dispatch.
  2. Implement domain tool modules (`system.py`, `reminders.py`, `automations.py`, `schedules.py`, `workflows.py`, `reasoner.py`).
  3. Decompose `Store` in `alfred/runtime.py` into domain helpers/mixins.
  4. Update `runtime.py`, `router.py`, `system2.py`, and `app.py` to use `ToolRegistry`.
  5. Update and run test suites.
- **Relevant Context**:
  - `alfred/runtime.py`: `Store`, `Tools`, `Scheduler`, `TOOL_SCHEMAS`
  - `alfred/automations.py`, `alfred/workflow_engine.py`
  - `tests/test_runtime.py`, `tests/test_automations.py`
- **Status**: [x] done

---

### Sub-Task 4: System2 & Qwen Plan Generation Cleanup
- **Intent**: Decouple embedded prompt strings, complex validation chains, and scattered approval logic in `System2` and `Qwen` into clean template definitions, typed plan structures, and cohesive approval store operations.
- **Expected Outcomes**:
  - Hardcoded multi-line system prompts extracted into clean template objects/constants in `alfred/prompts.py` or dedicated prompt modules.
  - Plan parsing and preflight checks structured into a cohesive `PlanValidator` class/function with clear error messages.
  - Approvals persistence and state management cleanly isolated from execution orchestration into `ApprovalStore`.
  - `tests/test_system2.py` and `tests/test_harness_bridge.py` pass cleanly.
- **Todo List**:
  1. Extract Qwen prompt templates and schema builder utilities out of inline method bodies into structured templates.
  2. Structure plan requests and approval records using typed Pydantic models / dataclasses.
  3. Simplify retry and response parsing logic in `Qwen._complete_admitted()` and `System2.parse_plan()`.
  4. Ensure error context is preserved when plan parsing fails.
  5. Run System2 and Qwen test suites.
- **Relevant Context**:
  - `alfred/models.py`: `Qwen`, `CactusNeedle`
  - `alfred/system2.py`: `System2`, `Approvals`, `parse_plan()`, `preflight()`
  - `tests/test_system2.py`, `tests/test_inference_admission.py`
- **Status**: [x] done

---

### Sub-Task 5: Alfred Composition Root & Property Delegation Modernization
- **Intent**: Clean up the `Alfred` application coordinator by removing all 30+ redundant property delegation boilerplate methods, establishing clean service dependency injection, and isolating watchdog thread management.
- **Expected Outcomes**:
  - `Alfred` class reduced to a clean, focused composition root initializing services in order.
  - Elimination of 30+ forwarding properties; callers access direct services (`alfred.router`, `alfred.store`, `alfred.tools`, etc.).
  - Watchdog lifecycle management encapsulated cleanly in a dedicated `WatchdogManager`.
  - `tests/test_alfred.py` and `tests/test_shutdown.py` updated and passing.
- **Todo List**:
  1. Remove all redundant property forwarding getters/setters in `Alfred` (`app.py`).
  2. Extract watchdog thread monitoring into a dedicated, testable `WatchdogManager`.
  3. Update direct property references in test suites to access service instances directly.
  4. Verify service initialization and shutdown invariants.
  5. Run test suite.
- **Relevant Context**:
  - `alfred/app.py`: `Alfred`
  - `tests/test_alfred.py`, `tests/test_shutdown.py`
- **Status**: [x] done

---

### Sub-Task 6: Error Handling & Exception Hierarchy Standardization
- **Intent**: Standardize error handling across the repository by eliminating bare `except Exception:` and silent `pass` blocks, introducing domain-specific error codes, and ensuring exception chaining (`from e`) retains root cause context.
- **Expected Outcomes**:
  - Zero bare `except:` or suppressed `except Exception: pass` blocks without explicit logging or handling.
  - Structured `ErrorCode` enumeration added to `alfred/exceptions.py`.
  - Robust exception propagation and wrapping for external I/O, subprocess, and model inference errors.
  - All exception and error-path tests pass.
- **Todo List**:
  1. Define standard `ErrorCode` constants in `alfred/exceptions.py`.
  2. Replace unhandled `except Exception:` blocks across router, runtime, cli, and backends with specific exception types and proper context logging.
  3. Remove dead or redundant exception suppression clauses.
  4. Verify error handling tests across all modules.
- **Relevant Context**:
  - `alfred/exceptions.py`
  - `alfred/router.py`, `alfred/runtime.py`, `alfred/backends.py`, `alfred/cli.py`
  - `tests/test_audio_failfast.py`, `tests/test_backends.py`
- **Status**: [x] done

---

### Sub-Task 7: Type Safety, Domain Dataclasses, and Constant Extraction Across Modules
- **Intent**: Replace untyped `dict[str, Any]` data envelopes with explicit dataclasses or Pydantic models for tool execution results, handoff requests, and session events, and extract magic strings/numbers into `constants.py`.
- **Expected Outcomes**:
  - Complete type hints (return types and parameters) on all public methods.
  - Structured dataclasses for `ToolResult`, `HandoffRequest`, and `PlanSummary`.
  - Magic strings replaced with named constants or enums across modules.
  - Clean linting and type checking across the codebase.
- **Todo List**:
  1. Define typed domain models for tool results and handoff payloads.
  2. Update method signatures across `runtime.py`, `router.py`, `system2.py`, and `models.py` with explicit type annotations.
  3. Centralize shared constants in `alfred/constants.py`.
  4. Verify that data serialization and deserialization remain clean with SQLite ledger and JSON logs.
- **Relevant Context**:
  - `alfred/constants.py`, `alfred/validators.py`, `alfred/cognitive/contracts.py`
  - `alfred/runtime.py`, `alfred/router.py`, `alfred/system2.py`
  - `tests/test_pydantic_models.py`, `tests/test_constants.py`
- **Status**: [x] done

---

### Sub-Task 8: Comprehensive Test Suite Validation and Regressions Sweep
- **Intent**: Execute the full pytest test suite across all modules, verify that code coverage remains intact, and validate clean code metrics.
- **Expected Outcomes**:
  - All unit, integration, and cognitive test suites pass with zero failures.
  - Cyclomatic complexity and function length metrics meet clean code standards.
  - Clean git status with formatted, well-documented code.
- **Todo List**:
  1. Run the complete pytest test suite across `tests/`.
  2. Verify all edge cases: offline routing, approval workflows, voice command gating, and error failfasts.
  3. Review all refactored modules against Clean Code heuristics (C1-C5, F1-F4, G1-G36, N1-N7, T1-T9).
- **Relevant Context**:
  - All test files in `tests/`
- **Status**: [x] done
