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
        """Requirements with source offsets. status: proposed, confirmed or rejected.

        `code` is derived from the source quote (^[A-Z]{2,4}-\\d{3}:), null otherwise.
        """
        return _run(
            "get_requirements",
            {"project_id": project_id, "status": status},
            lambda: tools.get_requirements(ctx, project_id, status),
        )

    @mcp.tool()
    def get_architecture(
        project_id: str, version: int | None = None, sections: list[str] | None = None
    ) -> dict[str, Any]:
        """Architecture proposal (latest approved by default), with lint_findings and status.

        Without `sections`: summary plus an index of sections with item counts. With `sections`
        (e.g. ["coverage_matrix"]): only those sections; cut lists are reported in
        `truncated_sections` as {name: {returned, total}}.
        """
        return _run(
            "get_architecture",
            {"project_id": project_id, "version": version, "sections": sections},
            lambda: tools.get_architecture(ctx, project_id, version, sections),
        )

    @mcp.tool()
    def get_plan(
        project_id: str, version: int | None = None, sections: list[str] | None = None
    ) -> dict[str, Any]:
        """Implementation plan (latest approved by default): ordered tasks with dependencies,
        requirement ids, acceptance criteria, flags and blocking questions. Each task has
        `dependencies` [{key, source: diagram | nfr rule | manual, reason?}]; `manual_edits` is the
        audit trail of manual dependency edits (and `previous_manual_edits` those of the prior
        version, which a rebuild does not carry over).

        Without `sections`: header plus an index of sections with item counts. With `sections`
        (tasks, manual_edits, unattached_questions, not_for_client): only those; cut lists are
        reported in `truncated_sections` as {name: {returned, total}}.
        """
        return _run(
            "get_plan",
            {"project_id": project_id, "version": version, "sections": sections},
            lambda: tools.get_plan(ctx, project_id, version, sections),
        )

    @mcp.tool()
    def get_patch(project_id: str, task_key: str, version: int | None = None) -> dict[str, Any]:
        """A proposed code patch for a task of the approved plan (default: latest accepted
        version, else the latest). Files, notes, assumptions, test_suggestions and warnings;
        cut sections are reported in `truncated_sections`. Read-only; code is never executed.
        """
        return _run(
            "get_patch",
            {"project_id": project_id, "task_key": task_key, "version": version},
            lambda: tools.get_patch(ctx, project_id, task_key, version),
        )

    @mcp.tool()
    def get_workspace_tree(project_id: str) -> dict[str, Any]:
        """The accepted generated files of a project (path, size, hash); capped at 500 entries."""
        return _run(
            "get_workspace_tree",
            {"project_id": project_id},
            lambda: tools.get_workspace_tree(ctx, project_id),
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
