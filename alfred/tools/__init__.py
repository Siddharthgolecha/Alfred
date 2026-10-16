"""Allowlisted tool definitions, schemas, and dynamic execution registry."""
from __future__ import annotations

from .base import ToolDefinition, ToolRegistry, default_registry, num_schema, schema, str_schema, tool
# Import domain modules to register tools in canonical order
from . import system, reminders, automations, schedules, workflows, reasoner

TOOL_SCHEMAS = default_registry.get_schemas()
SCHEMA_BY_NAME = default_registry.get_schema_by_name()
validate = default_registry.validate

__all__ = [
    "TOOL_SCHEMAS",
    "SCHEMA_BY_NAME",
    "ToolDefinition",
    "ToolRegistry",
    "default_registry",
    "num_schema",
    "schema",
    "str_schema",
    "tool",
    "validate",
]
