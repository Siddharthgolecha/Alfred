"""Recurring schedule tools: interval status checks, listing, and cancellation."""
from __future__ import annotations

from typing import Any

from .base import default_registry, num_schema


@default_registry.tool(
    name="schedule_every",
    description="Run a readonly Jetson status tool repeatedly at a fixed interval",
    properties={
        "tool": {"type": "string", "enum": ["get_gpu_status", "get_system_status"]},
        "interval_minutes": num_schema("Interval in minutes", 1, 10080),
    },
    required=["tool", "interval_minutes"],
)
def handle_schedule_every(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.store.create_schedule(args["tool"], args["interval_minutes"])


@default_registry.tool(
    name="list_schedules",
    description="List active recurring scheduled jobs",
)
def handle_list_schedules(context: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    return context.store.list_schedules()


@default_registry.tool(
    name="cancel_schedule",
    description="Cancel recurring job by numeric identifier",
    properties={"schedule_id": num_schema("Schedule id", 1, 1000000000)},
    required=["schedule_id"],
)
def handle_cancel_schedule(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.store.cancel_schedule(args["schedule_id"])
