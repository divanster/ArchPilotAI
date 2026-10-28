"""Read-only tool implementations. They only call existing read paths of the services."""

import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.architecture import ArchitectureContent, ProposalStatus
from app.domain.chunking import ChunkSettings
from app.domain.graph import MAX_IMPACT_DEPTH, ImpactMode
from app.domain.requirement import RequirementStatus
from app.domain.search import MAX_TOP_K, InvalidQueryError
from app.repositories.architecture import ArchitectureRepository
from app.repositories.chunk import ChunkRepository
from app.repositories.document import DocumentRepository
from app.repositories.graph import GraphRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture import ArchitectureService
from app.services.graph import GraphNodeNotFoundError, GraphService
from app.services.project import ProjectNotFoundError, ProjectService
from app.services.requirement import RequirementService
from app.services.retrieval import RetrievalService

logger = logging.getLogger("archpilot.mcp")

MAX_PROJECTS = 100
MAX_REQUIREMENTS = 100
MAX_ORPHANS = 100
MAX_IMPACT_HITS = 100
MAX_LIST_ITEMS = 20  # per nested list inside an architecture section
MAX_SECTION_ITEMS = 200  # per top-level architecture section list (coverage_matrix is the longest)
_REQUIREMENT_CODE = re.compile(r"^([A-Z]{2,4}-\d{3}):")
MAX_STRING_CHARS = 1000
MAX_QUOTE_CHARS = 500
MAX_ARCHITECTURE_CHARS = 60_000


class ToolInputError(Exception):
    """A caller mistake; the message is safe to show to the agent."""


class ReadOnlyViolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolContext:
    session_factory: Callable[[], Session]


def _refuse_flush(*_args: Any) -> None:
    raise ReadOnlyViolationError("The MCP server is read-only")


@contextmanager
def read_only_session(ctx: ToolContext) -> Iterator[Session]:
    """A session that refuses to flush anything and never commits."""
    session = ctx.session_factory()
    event.listen(session, "before_flush", _refuse_flush)
    try:
        yield session
    finally:
        session.close()  # rolls back any open transaction; loaded rows stay readable


def parse_uuid(value: str, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ToolInputError(f"{name} must be a UUID") from None


def _project(session: Session, project_id: str) -> uuid.UUID:
    pid = parse_uuid(project_id, "project_id")
    try:
        ProjectService(ProjectRepository(session)).get(pid)
    except ProjectNotFoundError:
        raise ToolInputError(f"Unknown project_id {pid}") from None
    return pid


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


def _cap(
    value: Any,
    path: str,
    report: dict[str, dict[str, int]],
    flag: list[bool],
    list_limit: int | None = None,
) -> Any:
    """Bound lists and strings in a JSON-like value. Cut lists are recorded in `report` by path."""
    if isinstance(value, str):
        text, cut = _truncate(value, MAX_STRING_CHARS)
        flag[0] = flag[0] or cut
        return text
    if isinstance(value, list):
        limit = MAX_LIST_ITEMS if list_limit is None else list_limit
        if len(value) > limit:
            report[path] = {"returned": limit, "total": len(value)}
        return [_cap(v, f"{path}[{i}]", report, flag) for i, v in enumerate(value[:limit])]
    if isinstance(value, dict):
        return {k: _cap(v, f"{path}.{k}", report, flag) for k, v in value.items()}
    return value


def _cap_section(name: str, value: Any, report: dict[str, dict[str, int]], flag: list[bool]) -> Any:
    """A requested section gets the full MAX_SECTION_ITEMS cap; nested lists use MAX_LIST_ITEMS."""
    return _cap(value, name, report, flag, MAX_SECTION_ITEMS)


def _node(node: Any) -> dict[str, Any]:
    return {
        "node_id": str(node.id),
        "kind": node.kind,
        "label": node.label,
        # A requirement or chunk id, or the item's field path inside the proposal.
        "source_ref": node.source_ref,
    }


def list_projects(ctx: ToolContext) -> dict[str, Any]:
    with read_only_session(ctx) as session:
        projects = list(ProjectService(ProjectRepository(session)).list())
    items = [
        {
            "project_id": str(p.id),
            "name": p.name,
            "description": _truncate(p.description or "", MAX_STRING_CHARS)[0],
            "status": str(getattr(p.status, "value", p.status)),
        }
        for p in projects[:MAX_PROJECTS]
    ]
    return {
        "projects": items,
        "total": len(projects),
        "truncated": len(projects) > MAX_PROJECTS,
    }


def _requirement_code(quote: str) -> str | None:
    match = _REQUIREMENT_CODE.match(quote)
    return match.group(1) if match else None


def get_requirements(
    ctx: ToolContext, project_id: str, status: str = "confirmed"
) -> dict[str, Any]:
    try:
        wanted = RequirementStatus(status)
    except ValueError:
        allowed = ", ".join(s.value for s in RequirementStatus)
        raise ToolInputError(f"status must be one of: {allowed}") from None
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        rows = list(
            RequirementService(RequirementRepository(session), ProjectRepository(session)).list(
                pid, wanted
            )
        )
    cut_quote = False
    items = []
    for r in rows[:MAX_REQUIREMENTS]:
        quote, cut = _truncate(r.source_quote, MAX_QUOTE_CHARS)
        cut_quote = cut_quote or cut
        items.append(
            {
                "requirement_id": str(r.id),
                "type": r.type,
                "title": r.title,
                "description": _truncate(r.description, MAX_STRING_CHARS)[0],
                "status": r.status,
                # Derived from the source text (e.g. "REQ-001: ..."), not stored; else null.
                "code": _requirement_code(r.source_quote),
                "source": {
                    "document_id": str(r.document_id),
                    "source_start": r.source_start,
                    "source_end": r.source_end,
                    "quote": quote,
                },
            }
        )
    return {
        "project_id": str(pid),
        "status": wanted.value,
        "requirements": items,
        "total": len(rows),
        "truncated": len(rows) > MAX_REQUIREMENTS or cut_quote,
    }


def _architecture_service(session: Session) -> ArchitectureService:
    return ArchitectureService(
        ArchitectureRepository(session), ProjectRepository(session), RequirementRepository(session)
    )


def get_architecture(
    ctx: ToolContext,
    project_id: str,
    version: int | None = None,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        service = _architecture_service(session)
        if version is None:
            approved = service.list(pid, ProposalStatus.APPROVED)
            if not approved:
                raise ToolInputError("The project has no approved architecture proposal")
            proposal = approved[0]  # newest version first
        else:
            matches = [p for p in service.list(pid) if p.version == version]
            if not matches:
                raise ToolInputError(f"The project has no architecture version {version}")
            proposal = matches[0]
        content = ArchitectureContent.model_validate(proposal.content).model_dump(mode="json")
        # Section names are the top-level keys of `content`, which are also the proposal field
        # paths; source_passages holds the document and offsets of every cited passage.
        valid = list(content)
        if sections:
            unknown = [s for s in sections if s not in content]
            if unknown:
                raise ToolInputError(
                    f"Unknown section(s): {', '.join(unknown)}. Valid sections: {', '.join(valid)}"
                )
        result: dict[str, Any] = {
            "project_id": str(pid),
            "proposal_id": str(proposal.id),
            "version": proposal.version,
            "status": proposal.status,
            "approval_status": proposal.status,
            "title": proposal.title,
            "summary": _truncate(proposal.summary, MAX_STRING_CHARS)[0],
            # None when the version was never linted.
            "lint_findings": proposal.lint_findings,
        }
        flag = [len(proposal.summary) > MAX_STRING_CHARS]
    report: dict[str, dict[str, int]] = {}
    result["lint_findings"] = _cap(result["lint_findings"], "lint_findings", report, flag)
    if not sections:
        result["sections_index"] = {
            name: {"items": len(v) if isinstance(v, list) else (None if v is None else 1)}
            for name, v in content.items()
        }
        result["hint"] = "Pass sections=[...] to get_architecture to fetch section content."
    else:
        wanted = set(sections)
        selected: dict[str, Any] = {}
        used = 0
        for name in valid:  # canonical order, whatever order the caller used
            if name not in wanted:
                continue
            value = _cap_section(name, content[name], report, flag)
            size = len(json.dumps(value, default=str))
            if isinstance(value, list) and used + size > MAX_ARCHITECTURE_CHARS:
                total = len(content[name])
                while value and used + len(json.dumps(value, default=str)) > MAX_ARCHITECTURE_CHARS:
                    value = value[: max(len(value) // 2, 0)] if len(value) > 1 else []
                report[name] = {"returned": len(value), "total": total}
                size = len(json.dumps(value, default=str))
            used += size
            selected[name] = value
        result["content"] = selected
    result["truncated_sections"] = report
    result["truncated"] = flag[0] or bool(report)
    return result


def _graph_service(session: Session) -> GraphService:
    return GraphService(
        GraphRepository(session),
        ArchitectureRepository(session),
        ProjectRepository(session),
        RequirementRepository(session),
        hub_threshold=get_settings().graph_hub_threshold,
    )


def _approved_graph(session: Session, pid: uuid.UUID) -> uuid.UUID:
    approved = _architecture_service(session).list(pid, ProposalStatus.APPROVED)
    if not approved:
        raise ToolInputError("The project has no approved architecture proposal")
    proposal_id = approved[0].id
    if not _graph_service(session).get(pid, proposal_id).built:
        raise ToolInputError("The traceability graph of the approved proposal is not built yet")
    return proposal_id


def get_impact(
    ctx: ToolContext, project_id: str, node_id: str, mode: str = "direct"
) -> dict[str, Any]:
    try:
        impact_mode = ImpactMode(mode)
    except ValueError:
        raise ToolInputError("mode must be 'direct' or 'transitive'") from None
    nid = parse_uuid(node_id, "node_id")
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        proposal_id = _approved_graph(session, pid)
        try:
            view = _graph_service(session).impact(
                pid, proposal_id, nid, depth=MAX_IMPACT_DEPTH, mode=impact_mode
            )
        except GraphNodeNotFoundError:
            raise ToolInputError(f"Unknown node_id {nid} in this project's graph") from None
        hits = [
            {
                **_node(view.nodes[h.node_id]),
                "depth": h.depth,
                "via_edge_kind": h.via_kind.value,
                "via_evidence": h.via_evidence,
                "reached_from": str(h.reached_from),
                "via_hub": h.via_hub,
            }
            for h in view.result.hits[:MAX_IMPACT_HITS]
            if h.node_id in view.nodes
        ]
        hubs = [
            {"node_id": str(h.node_id), "degree": h.degree, "label": view.nodes[h.node_id].label}
            for h in view.result.hubs
            if h.node_id in view.nodes
        ]
        return {
            "project_id": str(pid),
            "proposal_id": str(proposal_id),
            "mode": impact_mode.value,
            "root": _node(view.root),
            "impacted": hits,
            "hubs_not_expanded": hubs,
            "total": len(view.result.hits),
            "truncated": view.result.truncated or len(view.result.hits) > MAX_IMPACT_HITS,
        }


def get_orphans(ctx: ToolContext, project_id: str) -> dict[str, Any]:
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        proposal_id = _approved_graph(session, pid)
        found = _graph_service(session).orphans(pid, proposal_id)
        return {
            "project_id": str(pid),
            "proposal_id": str(proposal_id),
            "orphan_requirements": [_node(n) for n in found[:MAX_ORPHANS]],
            "total": len(found),
            "truncated": len(found) > MAX_ORPHANS,
        }


def search_documents(
    ctx: ToolContext, project_id: str, query: str, top_k: int = 5
) -> dict[str, Any]:
    settings = get_settings()
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        service = RetrievalService(
            ChunkRepository(session),
            DocumentRepository(session),
            ProjectRepository(session),
            ChunkSettings(settings.retrieval_chunk_size, settings.retrieval_chunk_overlap),
            settings.retrieval_max_scan_chunks,
        )
        try:
            outcome = service.search(pid, query, top_k=max(1, min(top_k, MAX_TOP_K)))
        except InvalidQueryError as exc:
            raise ToolInputError(str(exc)) from None
    cut_text = False
    hits = []
    for h in outcome.hits:
        text, cut = _truncate(h.text, MAX_STRING_CHARS)
        cut_text = cut_text or cut
        hits.append(
            {
                "document_id": str(h.document_id),
                "filename": h.filename,
                "chunk_index": h.chunk_index,
                "source_start": h.source_start,
                "source_end": h.source_end,
                "score": h.score,
                "matched_terms": list(h.matched_terms),
                "text": text,
            }
        )
    return {
        "project_id": str(pid),
        "hits": hits,
        "scanned_chunks": outcome.scanned_chunks,
        "unindexed_documents": outcome.unindexed_documents,
        "truncated": outcome.scan_limited or cut_text,
    }


def timed(name: str, args: dict[str, Any], call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Log tool name, argument values (never document text) and duration; hide unexpected errors."""
    start = time.perf_counter()
    outcome = "ok"
    try:
        return call()
    except ToolInputError:
        outcome = "rejected"
        raise
    except Exception:
        outcome = "error"
        logger.exception("mcp tool %s failed", name)
        raise ToolInputError("Internal error while reading project data") from None
    finally:
        logger.info(
            "mcp tool=%s args=%s outcome=%s duration_ms=%d",
            name,
            args,
            outcome,
            (time.perf_counter() - start) * 1000,
        )
