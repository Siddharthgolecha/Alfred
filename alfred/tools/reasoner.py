"""System 2 reasoning delegation tool."""
from __future__ import annotations

from typing import Any

from .base import default_registry, str_schema


@default_registry.tool(
    name="delegate_to_reasoner",
    description="Use the Qwen System 2 planner to answer an explanation or multi-step request",
    properties={"request": str_schema("Original spoken request or task")},
    required=["request"],
)
def handle_delegate_to_reasoner(context: Any, args: dict[str, Any]) -> Any:
    raise ValueError("Reasoner delegation is handled by the orchestrator")
