"""The read tools of the registry. Each handler calls the shared implementation in `tools`."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.mcp import tools
from app.mcp.tools import ToolContext
from app.tools.registry import Tool


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class ProjectArgs(_Args):
    project_id: str


class RequirementsArgs(_Args):
    project_id: str
    status: str = "confirmed"


class SectionsArgs(_Args):
    project_id: str
    version: int | None = None
    sections: list[str] | None = None


class PatchArgs(_Args):
    project_id: str
    task_key: str
    version: int | None = None


class ImpactArgs(_Args):
    project_id: str
    node_id: str
    mode: str = "direct"


class SearchArgs(_Args):
    project_id: str
    query: str
    top_k: int = 5


def _list_projects(ctx: ToolContext, _a: NoArgs) -> dict[str, Any]:
    return tools.list_projects(ctx)


def _get_requirements(ctx: ToolContext, a: RequirementsArgs) -> dict[str, Any]:
    return tools.get_requirements(ctx, a.project_id, a.status)


def _get_architecture(ctx: ToolContext, a: SectionsArgs) -> dict[str, Any]:
    return tools.get_architecture(ctx, a.project_id, a.version, a.sections)


def _get_plan(ctx: ToolContext, a: SectionsArgs) -> dict[str, Any]:
    return tools.get_plan(ctx, a.project_id, a.version, a.sections)


def _get_patch(ctx: ToolContext, a: PatchArgs) -> dict[str, Any]:
    return tools.get_patch(ctx, a.project_id, a.task_key, a.version)


def _get_workspace_tree(ctx: ToolContext, a: ProjectArgs) -> dict[str, Any]:
    return tools.get_workspace_tree(ctx, a.project_id)


def _get_impact(ctx: ToolContext, a: ImpactArgs) -> dict[str, Any]:
    return tools.get_impact(ctx, a.project_id, a.node_id, a.mode)


def _get_orphans(ctx: ToolContext, a: ProjectArgs) -> dict[str, Any]:
    return tools.get_orphans(ctx, a.project_id)


def _search_documents(ctx: ToolContext, a: SearchArgs) -> dict[str, Any]:
    return tools.search_documents(ctx, a.project_id, a.query, a.top_k)


def _search_log_args(a: SearchArgs) -> dict[str, Any]:
    # Never the query text itself.
    return {"project_id": a.project_id, "query_chars": len(a.query), "top_k": a.top_k}


class SymbolArgs(_Args):
    project_id: str
    name: str = Field(min_length=1, max_length=200)
    kind: str | None = Field(default=None, max_length=20)


class ReferencesArgs(_Args):
    project_id: str
    name: str = Field(min_length=1, max_length=200)


class DependantsArgs(_Args):
    project_id: str
    path: str = Field(min_length=1, max_length=500)
    transitive: bool = False


class RepoSearchArgs(_Args):
    project_id: str
    query: str = Field(min_length=2, max_length=200)
    prefix: str = Field(default="", max_length=300)


def _get_repo_summary(ctx: ToolContext, a: ProjectArgs) -> dict[str, Any]:
    return tools.get_repo_summary(ctx, a.project_id)


def _find_symbol(ctx: ToolContext, a: SymbolArgs) -> dict[str, Any]:
    return tools.find_symbol(ctx, a.project_id, a.name, a.kind)


def _find_references(ctx: ToolContext, a: ReferencesArgs) -> dict[str, Any]:
    return tools.find_references(ctx, a.project_id, a.name)


def _get_file_dependants(ctx: ToolContext, a: DependantsArgs) -> dict[str, Any]:
    return tools.get_file_dependants(ctx, a.project_id, a.path, a.transitive)


def _search_repository(ctx: ToolContext, a: RepoSearchArgs) -> dict[str, Any]:
    return tools.search_repository(ctx, a.project_id, a.query, a.prefix)


def _repo_search_log_args(a: RepoSearchArgs) -> dict[str, Any]:
    return {"project_id": a.project_id, "query_chars": len(a.query), "prefix": a.prefix[:200]}


def _read(name: str, description: str, model: type[BaseModel], handler: Any, **extra: Any) -> Tool:
    return Tool(name, description, "read", "mcp", model, handler, **extra)


READ_TOOLS: list[Tool] = [
    _read(
        "list_projects", "List projects (id, name, status). Capped at 100.", NoArgs, _list_projects
    ),
    _read(
        "get_requirements",
        "Requirements with source offsets. status: proposed, confirmed or rejected.\n\n"
        "`code` is derived from the source quote (^[A-Z]{2,4}-\\d{3}:), null otherwise.",
        RequirementsArgs,
        _get_requirements,
    ),
    _read(
        "get_architecture",
        "Architecture proposal (latest approved by default), with lint_findings and status.\n\n"
        "Without `sections`: summary plus an index of sections with item counts. With `sections`\n"
        '(e.g. ["coverage_matrix"]): only those sections; cut lists are reported in\n'
        "`truncated_sections` as {name: {returned, total}}.",
        SectionsArgs,
        _get_architecture,
    ),
    _read(
        "get_plan",
        "Implementation plan (latest approved by default): ordered tasks with dependencies,\n"
        "requirement ids, acceptance criteria, flags and blocking questions. Each task has\n"
        "`dependencies` [{key, source: diagram | nfr rule | manual, reason?}]; `manual_edits` is\n"
        "the audit trail of manual dependency edits (and `previous_manual_edits` those of the\n"
        "prior version, which a rebuild does not carry over).\n\n"
        "Without `sections`: header plus an index of sections with item counts. With `sections`\n"
        "(tasks, manual_edits, unattached_questions, not_for_client): only those; cut lists are\n"
        "reported in `truncated_sections` as {name: {returned, total}}.",
        SectionsArgs,
        _get_plan,
    ),
    _read(
        "get_patch",
        "A proposed code patch for a task of the approved plan (default: latest accepted\n"
        "version, else the latest). Files, notes, assumptions, test_suggestions and warnings;\n"
        "cut sections are reported in `truncated_sections`. Read-only; code is never executed.",
        PatchArgs,
        _get_patch,
    ),
    _read(
        "get_workspace_tree",
        "The accepted generated files of a project (path, size, hash); capped at 500 entries.",
        ProjectArgs,
        _get_workspace_tree,
    ),
    _read(
        "get_impact",
        "What depends on a graph node of the approved architecture. mode: direct or transitive.",
        ImpactArgs,
        _get_impact,
    ),
    _read(
        "get_orphans",
        "Confirmed requirements not covered by the approved architecture.",
        ProjectArgs,
        _get_orphans,
    ),
    _read(
        "search_documents",
        "BM25 keyword search over the project's indexed documents (top_k at most 20).",
        SearchArgs,
        _search_documents,
        log_args=_search_log_args,
    ),
    _read(
        "get_repo_summary",
        "Code map of the project's connected repository: languages, frameworks, manifests,\n"
        "entry points, source/test roots, generated and excluded file counts, scripts and\n"
        "lint/test/build commands, instruction files, scan revision and whether it is stale.\n"
        "Read-only; repository code is never executed.",
        ProjectArgs,
        _get_repo_summary,
    ),
    _read(
        "find_symbol",
        "Where a function, class, method, type or variable is defined in the connected\n"
        "repository (exact name, else partial). kind filters: function, class, method,\n"
        "interface, type, enum, variable.",
        SymbolArgs,
        _find_symbol,
    ),
    _read(
        "find_references",
        "Whole-word occurrences of an identifier in the connected repository's source and\n"
        "test files, marked definition, import or use; capped at 100.",
        ReferencesArgs,
        _find_references,
    ),
    _read(
        "get_file_dependants",
        "The files a repository file imports and the files that import it (Python and\n"
        "TypeScript/JavaScript relative imports); transitive=true follows dependants further.",
        DependantsArgs,
        _get_file_dependants,
    ),
    _read(
        "search_repository",
        "Literal, case-insensitive search of the connected repository's mapped text files\n"
        "(generated, secret and excluded files are never searched); optional path prefix.",
        RepoSearchArgs,
        _search_repository,
        log_args=_repo_search_log_args,
    ),
]
