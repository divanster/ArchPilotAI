from app.tools.registry import Tool, ToolInputError, ToolRegistry, UnknownToolError

__all__ = ["Tool", "ToolInputError", "ToolRegistry", "UnknownToolError", "get_registry"]

_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """The shared registry, built on first use (the tool modules import services)."""
    global _registry
    if _registry is None:
        from app.mcp.read_tools import READ_TOOLS
        from app.services.agent_tools import AGENT_TOOLS

        _registry = ToolRegistry([*READ_TOOLS, *AGENT_TOOLS])
    return _registry
