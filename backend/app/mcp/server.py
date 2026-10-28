"""Read-only MCP server over stdio. Run with `python -m app.mcp`."""

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from app.core.config import get_settings
from app.core.db import get_database, make_session_factory
from app.core.logging import configure_logging
from app.mcp import tools
from app.mcp.tools import ToolContext, ToolInputError


def _run(name: str, args: dict[str, Any], call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return tools.timed(name, args, call)
    except ToolInputError as exc:
        raise ToolError(str(exc)) from None


def create_server(ctx: ToolContext) -> FastMCP:
    mcp = FastMCP("archpilotai", instructions="Read-only access to ArchPilotAI project knowledge.")

    @mcp.tool()
    def list_projects() -> dict[str, Any]:
        """List projects (id, name, status). Capped at 100."""
        return _run("list_projects", {}, lambda: tools.list_projects(ctx))

    @mcp.tool()
    def get_requirements(project_id: str, status: str = "confirmed") -> dict[str, Any]:
        """Requirements with source offsets. status: proposed, confirmed or rejected."""
        return _run(
            "get_requirements",
            {"project_id": project_id, "status": status},
            lambda: tools.get_requirements(ctx, project_id, status),
        )

    @mcp.tool()
    def get_architecture(project_id: str, version: int | None = None) -> dict[str, Any]:
        """Architecture proposal (latest approved by default) with source passages."""
        return _run(
            "get_architecture",
            {"project_id": project_id, "version": version},
            lambda: tools.get_architecture(ctx, project_id, version),
        )

    @mcp.tool()
    def get_impact(project_id: str, node_id: str, mode: str = "direct") -> dict[str, Any]:
        """What depends on a graph node of the approved architecture. mode: direct or transitive."""
        return _run(
            "get_impact",
            {"project_id": project_id, "node_id": node_id, "mode": mode},
            lambda: tools.get_impact(ctx, project_id, node_id, mode),
        )

    @mcp.tool()
    def get_orphans(project_id: str) -> dict[str, Any]:
        """Confirmed requirements not covered by the approved architecture."""
        return _run(
            "get_orphans", {"project_id": project_id}, lambda: tools.get_orphans(ctx, project_id)
        )

    @mcp.tool()
    def search_documents(project_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
        """BM25 keyword search over the project's indexed documents (top_k at most 20)."""
        return _run(
            "search_documents",
            {"project_id": project_id, "query_chars": len(query), "top_k": top_k},
            lambda: tools.search_documents(ctx, project_id, query, top_k),
        )

    return mcp


def main() -> None:
    configure_logging(get_settings().log_level)
    factory = make_session_factory(get_database().engine)
    create_server(ToolContext(factory)).run(transport="stdio")
