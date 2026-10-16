"""Workflow execution and listing tools."""
from __future__ import annotations

import json
from typing import Any

from .base import default_registry, str_schema


@default_registry.tool(
    name="run_workflow",
    description="Execute a previously saved and validated named workflow",
    properties={"name": str_schema("Existing workflow name")},
    required=["name"],
)
def handle_run_workflow(context: Any, args: dict[str, Any]) -> list[Any]:
    return context.run_plan(context.store.get_workflow(args["name"]))


@default_registry.tool(
    name="list_workflows",
    description="List saved named workflows",
)
def handle_list_workflows(context: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": workflow["name"], "steps": len(json.loads(workflow["steps_json"]))}
        for workflow in context.store.list_workflows()
    ]
