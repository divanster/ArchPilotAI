"""The agent's tools in the registry: read tools and draft tools of one run's draft workspace.

Handlers get an `AgentCall` (the run's state) and the validated arguments. They raise
`ActionError` for refusals; the loop turns those into step results.
"""

from dataclasses import dataclass
from typing import Any

from app.domain.agent import (
    ARG_MODELS,
    MAX_READ_CHARS,
    ActionError,
    Draft,
    DraftFile,
    cut,
    search_draft,
)
from app.domain.execution import attribute
from app.domain.patch import path_problem
from app.mcp import tools
from app.tools.registry import Tier, Tool


@dataclass
class AgentCall:
    svc: Any  # the AgentService running the loop
    project_id: Any
    plan: Any
    task: Any
    draft: Draft
    run: Any
    warnings: list[str]
    latency_ms: int
    provider: str
    model: str
    based_on: int | None = None
    done: bool = False  # set by the tool that ends the run


def _existing(draft: Draft, path: str) -> DraftFile:
    problem = path_problem(path)
    if problem:
        raise ActionError(problem, f"Refused: {problem}", path=path)
    file = draft.get(path)
    if file is None:
        raise ActionError("file_not_found", path=path)
    return file


def _refuse_migration(call: AgentCall, path: str) -> None:
    rules = call.svc.patches.frameworks(call.project_id, call.draft.live())
    if rules.refuses(path):
        raise ActionError(
            "django_migration",
            f"Refused: Django migrations are not written by hand. {rules.note()}",
            path=path,
            data={"note": rules.note()},
        )


def read_file(call: AgentCall, a: Any) -> dict[str, Any]:
    file = _existing(call.draft, a.path)
    lines = file.content.splitlines()
    start = a.start_line or 1
    end = min(a.end_line or len(lines), len(lines))
    text = "\n".join(lines[start - 1 : end]) if lines else ""
    body = cut(text, MAX_READ_CHARS)
    return {
        "summary": f"read {max(end - start + 1, 0)} line(s)",
        "path": a.path,
        "content": body,
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "truncated": len(body) < len(text),
    }


def list_files(call: AgentCall, a: Any) -> dict[str, Any]:
    draft = call.draft
    paths = sorted(p for p in draft.live() if p.startswith(a.prefix))
    shown = paths[: a.limit]
    return {
        "summary": f"{len(shown)} of {len(paths)} file(s)",
        "files": [{"path": p, "bytes": len(draft.files[p].content.encode())} for p in shown],
        "total": len(paths),
        "truncated": len(paths) > len(shown),
    }


def search_code(call: AgentCall, a: Any) -> dict[str, Any]:
    matches, total, truncated = search_draft(call.draft, a.query, a.prefix)
    return {
        "summary": f"{total} match(es)",
        "matches": matches,
        "total": total,
        "truncated": truncated,
    }


def get_requirements(call: AgentCall, _a: Any) -> dict[str, Any]:
    reqs = call.svc.requirements(call.project_id, call.task)
    return {"summary": f"{len(reqs)} requirement(s)", "requirements": reqs}


def get_task(call: AgentCall, a: Any) -> dict[str, Any]:
    return call.svc.task_view(call.project_id, call.plan, call.task, a.key)  # type: ignore[no-any-return]


def get_impact(call: AgentCall, a: Any) -> dict[str, Any]:
    try:
        found = tools.get_impact(call.svc.tool_context, str(call.project_id), a.node_id, a.mode)
    except tools.ToolInputError as exc:
        raise ActionError("tool_input", str(exc)) from None
    return {"summary": f"{found['total']} impacted node(s)", **found}


def edit_file(call: AgentCall, a: Any) -> dict[str, Any]:
    _refuse_migration(call, a.path)
    file = _existing(call.draft, a.path)
    count = file.content.count(a.search)
    if count == 0:
        raise ActionError("no_match", path=a.path)
    if count > 1:
        raise ActionError("multiple_matches", path=a.path, data={"matches": count})
    new = file.content.replace(a.search, a.replace, 1)
    if new == file.content:
        raise ActionError("unchanged", path=a.path)
    problem = call.svc.write_problem(a.path, new)
    if problem:
        raise ActionError(problem, f"Refused: {problem}", path=a.path)
    call.draft.put(a.path, new)
    return {"summary": "edited 1 occurrence", "path": a.path, "bytes": len(new.encode())}


def write_file(call: AgentCall, a: Any) -> dict[str, Any]:
    _refuse_migration(call, a.path)
    problem = call.svc.write_problem(a.path, a.content)
    if problem:
        raise ActionError(problem, f"Refused: {problem}", path=a.path)
    if call.draft.clash(a.path) is not None:
        raise ActionError("file_exists", path=a.path)
    call.draft.put(a.path, a.content)
    return {
        "summary": f"wrote {len(a.content.encode())} byte(s)",
        "path": a.path,
        "bytes": len(a.content.encode()),
    }


def delete_file(call: AgentCall, a: Any) -> dict[str, Any]:
    _refuse_migration(call, a.path)
    _existing(call.draft, a.path)
    call.draft.delete(a.path)
    return {"summary": "deleted", "path": a.path}


def run_static_checks(call: AgentCall, _a: Any) -> dict[str, Any]:
    report = call.svc.checker.run({p: f.content for p, f in call.draft.live().items()})
    if report.unavailable:
        # Never "0 findings" when a checker did not run.
        raise ActionError(
            "checks_unavailable",
            f"Static checker unavailable: {', '.join(report.unavailable)}",
            data={"status": "unavailable", "unavailable": report.unavailable},
        )
    data = report.as_json()
    changed = [c.path for c in call.draft.changes()]
    tagged, counts = attribute(data.get("findings") or [], changed)
    data["findings"] = tagged
    data["scope"] = counts
    return {"summary": f"{report.total} finding(s)", **data}


def run_tests(call: AgentCall, a: Any) -> dict[str, Any]:
    return call.svc.run_tests(  # type: ignore[no-any-return]
        a.command_id, call.project_id, call.draft, call.run, call.warnings
    )


def finish(call: AgentCall, a: Any) -> dict[str, Any]:
    result, call.done = call.svc.finish_draft(
        a,
        call.project_id,
        call.plan,
        call.task,
        call.draft,
        call.run,
        call.warnings,
        call.latency_ms,
        call.provider,
        call.model,
        call.based_on,
    )
    return result  # type: ignore[no-any-return]


def _tool(name: str, tier: Tier, handler: Any, description: str, **extra: Any) -> Tool:
    return Tool(
        name, description, tier, "agent", ARG_MODELS[name], handler, log_args=_log_args, **extra
    )


def _log_args(args: Any) -> dict[str, Any]:
    """Step arguments for the log: sizes instead of contents, short strings only."""
    out: dict[str, Any] = {}
    for key, value in args.model_dump().items():
        if key in ("content", "search", "replace"):
            out[f"{key}_chars"] = len(value)
        elif isinstance(value, str):
            out[key] = cut(value, 200)
        elif isinstance(value, list):
            out[key] = len(value)
        else:
            out[key] = value
    return out


AGENT_TOOLS: list[Tool] = [
    _tool("read_file", "read", read_file, "Read lines of a draft file."),
    _tool("list_files", "read", list_files, "List draft files under a prefix."),
    _tool("search_code", "read", search_code, "Literal, case-insensitive search of the draft."),
    _tool(
        "get_requirements",
        "read",
        get_requirements,
        "The confirmed requirements of this task.",
    ),
    _tool(
        "get_task",
        "read",
        get_task,
        "A task of the plan (default: this one).",
    ),
    _tool(
        "get_impact",
        "read",
        get_impact,
        "What depends on a graph node of the approved architecture; mode: direct or transitive.",
    ),
    _tool(
        "edit_file",
        "draft",
        edit_file,
        '"search" must match EXACTLY ONCE in the file.',
    ),
    _tool("write_file", "draft", write_file, "Write a new file; new files only."),
    _tool("delete_file", "draft", delete_file, "Delete a draft file."),
    _tool(
        "run_static_checks",
        "draft",
        run_static_checks,
        "ruff, mypy, tsc --noEmit and syntax parsing of the draft; no tests run.",
    ),
    _tool(
        "run_tests",
        "draft",
        run_tests,
        'Only when the message lists "run_tests"; command_id is pytest, ruff, mypy, tsc or '
        '"npm test". You get new failures in changed files only; elsewhere and pre-existing '
        "are counted.",
        needs_execution=True,
    ),
    _tool("finish", "draft", finish, "Turns the draft into a patch for review; ends the run."),
]
