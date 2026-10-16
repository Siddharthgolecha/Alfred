"""Automations management: GPU temperature watch, pause, resume, cancel, and listing."""
from __future__ import annotations

from typing import Any

from .base import default_registry, num_schema


@default_registry.tool(
    name="watch_gpu_temperature",
    description="Create recurring read-only GPU temperature alert; requires approval",
    properties={
        "threshold_c": num_schema("Alert at or above Celsius threshold", 20, 110),
        "interval_minutes": num_schema("Check frequency, minutes", 1, 1440),
        "cooldown_minutes": num_schema("Minimum alert cooldown minutes", 1, 10080),
    },
    required=["threshold_c", "interval_minutes", "cooldown_minutes"],
)
def handle_watch_gpu_temperature(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.automations.watch_gpu(
        args["threshold_c"], args["interval_minutes"], args["cooldown_minutes"]
    )


@default_registry.tool(
    name="list_automations",
    description="List active and paused absolute/daily reminders and GPU temperature watches",
)
def handle_list_automations(context: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    return context.automations.list_jobs()


@default_registry.tool(
    name="pause_automation",
    description="Pause a Phase 3 automation by numeric ID",
    properties={"automation_id": num_schema("Automation ID", 1, 1000000000)},
    required=["automation_id"],
)
def handle_pause_automation(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.automations.change(args["automation_id"], "pause")


@default_registry.tool(
    name="resume_automation",
    description="Resume a paused Phase 3 automation by numeric ID",
    properties={"automation_id": num_schema("Automation ID", 1, 1000000000)},
    required=["automation_id"],
)
def handle_resume_automation(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.automations.change(args["automation_id"], "resume")


@default_registry.tool(
    name="cancel_automation",
    description="Cancel a Phase 3 automation by numeric ID",
    properties={"automation_id": num_schema("Automation ID", 1, 1000000000)},
    required=["automation_id"],
)
def handle_cancel_automation(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.automations.change(args["automation_id"], "cancel")
