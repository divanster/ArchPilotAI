"""Read-only MCP server over stdio. Run with `python -m app.mcp`."""

import inspect
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from app.core.config import get_settings
from app.core.db import get_database, make_session_factory
from app.core.logging import configure_logging
from app.mcp import tools
from app.mcp.tools import ToolContext, ToolInputError
from app.tools import Tool, ToolRegistry, get_registry


def _run(name: str, args: dict[str, Any], call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return tools.timed(name, args, call)
    except ToolInputError as exc:
        raise ToolError(str(exc)) from None


def _bind(tool: Tool, registry: ToolRegistry, ctx: ToolContext) -> Callable[..., dict[str, Any]]:
    """A function whose signature is the tool's argument model, for FastMCP's schema."""
    params = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if field.is_required() else field.default,
            annotation=field.annotation,
        )
        for name, field in tool.args_model.model_fields.items()
    ]

    def call(**kwargs: Any) -> dict[str, Any]:
        args = tool.args_model.model_validate(kwargs)
        logged = tool.log_args(args) if tool.log_args else args.model_dump()
        return _run(tool.name, logged, lambda: registry.call("mcp", tool.name, kwargs, ctx))

    call.__name__ = tool.name
    call.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    return call


def create_server(ctx: ToolContext, registry: ToolRegistry | None = None) -> FastMCP:
    registry = registry or get_registry()
    mcp = FastMCP("archpilotai", instructions="Read-only access to ArchPilotAI project knowledge.")
    for tool in registry.exposed("mcp"):
        mcp.add_tool(_bind(tool, registry, ctx), name=tool.name, description=tool.description)
    return mcp


def main() -> None:
    configure_logging(get_settings().log_level)
    factory = make_session_factory(get_database().engine)
    create_server(ToolContext(factory)).run(transport="stdio")
