"""Durable reminder creation, absolute/daily reminders, and cancellation tools."""
from __future__ import annotations

from typing import Any

from .base import default_registry, num_schema, str_schema


@default_registry.tool(
    name="create_reminder",
    description="Create an alert in a number of minutes",
    properties={
        "message": str_schema("Reminder text"),
        "delay_minutes": num_schema("Delay in minutes", 1, 10080),
    },
    required=["message", "delay_minutes"],
)
def handle_create_reminder(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    if not args["message"].strip():
        raise ValueError("Reminder cannot be blank")
    return context.store.create_reminder(args["message"], args["delay_minutes"])


@default_registry.tool(
    name="create_reminder_at",
    description="Remind at an explicit ISO-8601 date-time including timezone offset; example 2026-10-15T09:00:00+05:30",
    properties={
        "message": str_schema("Reminder message"),
        "when": str_schema("ISO-8601 date and clock time with +05:30 or Z"),
    },
    required=["message", "when"],
)
def handle_create_reminder_at(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.automations.remind_at(args["message"], args["when"])


@default_registry.tool(
    name="create_daily_reminder",
    description="Create a daily reminder at a local clock time in an explicit IANA timezone",
    properties={
        "message": str_schema("Reminder message"),
        "local_time": str_schema("HH:MM 24-hour clock"),
        "timezone": str_schema("IANA timezone such as Asia/Kolkata"),
    },
    required=["message", "local_time", "timezone"],
)
def handle_create_daily_reminder(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.automations.remind_daily(
        args["message"], args["local_time"], args["timezone"]
    )


@default_registry.tool(
    name="list_reminders",
    description="List scheduled pending reminders",
)
def handle_list_reminders(context: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    return context.store.list_reminders()


@default_registry.tool(
    name="cancel_reminder",
    description="Cancel reminder by numeric identifier",
    properties={"reminder_id": num_schema("Reminder id", 1, 1000000000)},
    required=["reminder_id"],
)
def handle_cancel_reminder(context: Any, args: dict[str, Any]) -> dict[str, Any]:
    return context.store.cancel_reminder(args["reminder_id"])
