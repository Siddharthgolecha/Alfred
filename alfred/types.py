"""Domain dataclasses for tool results, plan summaries, and handoff requests.

These types replace untyped ``dict`` returns at key API boundaries so that
static analysis and mypy can catch misuse early. They are plain frozen
dataclasses — no Pydantic overhead — since they are internal coordination
types, not persisted or serialised contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    """The direct output of a single tool execution.

    ``value`` is the raw dict/list returned by the tool handler.  ``skipped``
    is ``True`` when a conditional ``when`` predicate evaluated to ``False``
    and the tool was deliberately not run.
    """

    tool: str
    value: Any
    skipped: bool = False

    @classmethod
    def from_plan_step(cls, tool: str, raw: Any) -> "ToolResult":
        """Wrap a ``run_plan`` result entry into a typed ToolResult."""
        skipped = isinstance(raw, dict) and raw.get("skipped") is True
        return cls(tool=tool, value=raw, skipped=skipped)


@dataclass(frozen=True)
class PlanSummary:
    """A parsed and pre-flighted Qwen / Harness plan ready for execution.

    ``steps`` is the validated list of ``{"tool": ..., "args": ..., ...}``
    dicts as returned by ``parse_plan`` / ``preflight``.  ``message`` is the
    optional human-readable description from the planner.  ``save_as`` is set
    when the plan requests saving the steps as a named workflow.
    """

    steps: tuple[dict[str, Any], ...]
    message: str = ""
    save_as: str | None = None

    @classmethod
    def from_parse(
        cls,
        message: str,
        steps: list[dict[str, Any]],
        save_as: str | None,
    ) -> "PlanSummary":
        """Construct from the three-tuple returned by ``parse_plan``."""
        return cls(steps=tuple(steps), message=message, save_as=save_as)

    @property
    def step_names(self) -> tuple[str, ...]:
        """Return the tool names for all steps in order."""
        return tuple(s["tool"] for s in self.steps)


@dataclass(frozen=True)
class HandoffRequest:
    """A structured escalation request from Needle 3 to System 2 (Qwen).

    Captures exactly the information needed to resume the authorised task on
    the Qwen side: the session/task IDs, the user's original utterance, the
    completed read-only prefix, and access-control flags.
    """

    session_id: str
    task_id: str
    original_text: str
    reason: str
    completed_calls: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    completed_results: tuple[Any, ...] = field(default_factory=tuple)
    allow_actions: bool = False
    used_decisions: int = 1

    @classmethod
    def build(
        cls,
        session_id: str,
        task_id: str,
        original_text: str,
        reason: str,
        *,
        calls: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        results: list[Any] | tuple[Any, ...] = (),
        allow_actions: bool = False,
        used_decisions: int = 1,
    ) -> "HandoffRequest":
        """Convenience constructor that normalises lists to tuples."""
        return cls(
            session_id=session_id,
            task_id=task_id,
            original_text=original_text,
            reason=reason,
            completed_calls=tuple(calls),
            completed_results=tuple(results),
            allow_actions=allow_actions,
            used_decisions=used_decisions,
        )
