"""Core tool registration decorator, schema generation, and tool catalog."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any

from ..constants import Limits

LOG = logging.getLogger(__name__)


def schema(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Return a JSON Schema tool definition object."""
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


def str_schema(description: str) -> dict[str, Any]:
    """Return a JSON Schema string property with standard max length."""
    return {"type": "string", "description": description, "maxLength": Limits.TOOL_STR_MAX_LENGTH}


def num_schema(description: str, lo: int, hi: int) -> dict[str, Any]:
    """Return a JSON Schema integer property with an explicit range."""
    return {"type": "integer", "description": description, "minimum": lo, "maximum": hi}


@dataclass
class ToolDefinition:
    """Metadata, JSON schema, and execution handler for an allowlisted tool."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[Any, dict[str, Any]], Any] | None = None


class ToolRegistry:
    """Registry managing tool definitions, schema validation, and execution dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        description: str,
        properties: dict[str, Any] | None = None,
        required: list[str] | None = None,
        schema_dict: dict[str, Any] | None = None,
    ) -> Callable[[Callable[[Any, dict[str, Any]], Any]], Callable[[Any, dict[str, Any]], Any]]:
        """Decorator to register a tool handler with its parameter schema."""
        tool_schema = schema_dict or schema(name, description, properties, required)

        def decorator(
            handler: Callable[[Any, dict[str, Any]], Any]
        ) -> Callable[[Any, dict[str, Any]], Any]:
            self._tools[name] = ToolDefinition(
                name=name,
                description=description,
                schema=tool_schema,
                handler=handler,
            )
            return handler

        return decorator

    def tool(
        self,
        name: str,
        description: str,
        properties: dict[str, Any] | None = None,
        required: list[str] | None = None,
        schema_dict: dict[str, Any] | None = None,
    ) -> Callable[[Callable[[Any, dict[str, Any]], Any]], Callable[[Any, dict[str, Any]], Any]]:
        """Alias for register decorator."""
        return self.register(name, description, properties, required, schema_dict)

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return all registered tool JSON schemas."""
        return [tool.schema for tool in self._tools.values()]

    def get_schema_by_name(self) -> dict[str, dict[str, Any]]:
        """Return a mapping of tool name to schema dictionary."""
        return {name: tool.schema for name, tool in self._tools.items()}

    def validate(self, name: str, args: dict[str, Any], allow_delegate: bool = True) -> None:
        """Validate tool name and arguments against registered JSON schemas."""
        schemas = self.get_schema_by_name()
        if name not in schemas:
            raise ValueError(f"Tool not allowlisted: {name}")
        if name == "delegate_to_reasoner" and not allow_delegate:
            raise ValueError("Nested reasoning is not allowed")
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")

        params = schemas[name]["parameters"]
        for k in args:
            if k not in params["properties"]:
                raise ValueError(f"Unexpected argument for {name}")
        for k in params["required"]:
            if k not in args:
                raise ValueError(f"Missing argument for {name}")

        for k, v in args.items():
            d = params["properties"][k]
            if d["type"] == "integer" and (
                type(v) is not int or not d["minimum"] <= v <= d["maximum"]
            ):
                raise ValueError(f"Invalid integer {k}")
            if d["type"] == "string" and (
                not isinstance(v, str)
                or len(v) > d.get("maxLength", Limits.TOOL_STR_MAX_LENGTH)
                or (d.get("enum") and v not in d["enum"])
            ):
                raise ValueError(f"Invalid string {k}")

    def execute(self, name: str, args: dict[str, Any], context: Any) -> Any:
        """Dispatch execution to the registered tool handler."""
        tool_def = self._tools.get(name)
        if tool_def is None or tool_def.handler is None:
            raise PermissionError(f"Unknown tool: {name}")
        return tool_def.handler(context, args)


default_registry = ToolRegistry()
tool = default_registry.tool
