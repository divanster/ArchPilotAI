"""The one tool registry shared by the MCP server and the agent loop.

A tool has a name, an argument model (its JSON schema), a handler, a tier and a description.
Tiers: "read" (changes nothing), "draft" (changes only the agent's draft workspace) and
"approval" (needs a person; defined for the future). What an audience may see is decided here
and nowhere else; "approval" tools are never exposed to either audience.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

Tier = Literal["read", "draft", "approval"]
Audience = Literal["mcp", "agent"]

EXPOSED_TIERS: dict[str, frozenset[str]] = {
    "mcp": frozenset({"read"}),
    "agent": frozenset({"read", "draft"}),
}


class ToolInputError(Exception):
    """A caller mistake; the message is short and safe to show to the caller."""


class UnknownToolError(ToolInputError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown tool: {name[:60]}")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    tier: Tier
    audience: Audience
    args_model: type[BaseModel]
    handler: Callable[[Any, Any], dict[str, Any]]
    # Offered to the model only when a real execution provider exists.
    needs_execution: bool = False
    # Argument values for the log (sizes instead of contents); default: all of them.
    log_args: Callable[[Any], dict[str, Any]] | None = None

    def schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[tuple[str, str], Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        key = (tool.audience, tool.name)
        if key in self._tools:
            raise ValueError(f"Duplicate tool {tool.name!r} for {tool.audience}")
        self._tools[key] = tool

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def find(self, audience: str, name: str) -> Tool | None:
        """Any registered tool, whatever its tier (for display, never for dispatch)."""
        return self._tools.get((audience, name))

    def exposed(self, audience: str) -> list[Tool]:
        allowed = EXPOSED_TIERS.get(audience, frozenset())
        return [t for t in self._tools.values() if t.audience == audience and t.tier in allowed]

    def get(self, audience: str, name: str) -> Tool:
        tool = self._tools.get((audience, name))
        if tool is None or tool.tier not in EXPOSED_TIERS.get(audience, frozenset()):
            raise UnknownToolError(name)
        return tool

    def call(self, audience: str, name: str, raw_args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        tool = self.get(audience, name)
        try:
            args = tool.args_model.model_validate(raw_args)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}
            )
            raise ToolInputError(
                f"Invalid arguments for {tool.name}: {', '.join(fields[:5])}"
            ) from None
        return tool.handler(ctx, args)


def _type_of(spec: dict[str, Any]) -> str:
    if "anyOf" in spec:
        kinds = [_type_of(s) for s in spec["anyOf"] if s.get("type") != "null"]
        return "|".join(kinds) or "any"
    kind = str(spec.get("type", "any"))
    if kind == "array":
        return _type_of(spec.get("items", {})) + "[]"
    return kind


def render_args(tool: Tool) -> str:
    """Compact argument schema for a prompt: {"path": string, "start_line"?: integer}."""
    schema = tool.schema()
    required = set(schema.get("required", []))
    parts = [
        f'"{name}"{"" if name in required else "?"}: {_type_of(spec)}'
        for name, spec in schema.get("properties", {}).items()
    ]
    return "{" + ", ".join(parts) + "}"
