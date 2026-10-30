"""Read-only tool implementations. They only call existing read paths of the services."""

import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.architecture import ArchitectureContent, ProposalStatus
from app.domain.chunking import ChunkSettings
from app.domain.decisions import BLOCK_LABEL
from app.domain.graph import MAX_IMPACT_DEPTH, ImpactMode
from app.domain.patch import task_status
from app.domain.requirement import RequirementStatus
from app.domain.search import MAX_TOP_K, InvalidQueryError
from app.embeddings.contracts import EmbeddingProvider
from app.repositories.architecture import ArchitectureRepository
from app.repositories.chunk import ChunkRepository
from app.repositories.decision import DecisionRepository
from app.repositories.document import DocumentRepository
from app.repositories.embedding import EmbeddingRepository
from app.repositories.graph import GraphRepository
from app.repositories.patch import PatchRepository
from app.repositories.plan import PlanRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services import repo_map as repo_queries
from app.services.architecture import ArchitectureService
from app.services.embedding import space_of
from app.services.graph import GraphNodeNotFoundError, GraphService
from app.services.hybrid import HybridRetrievalService
from app.services.plan import PlanService
from app.services.project import ProjectNotFoundError, ProjectService
from app.services.repo_map import (
    RepoMap,
    RepoMapService,
    RepoQueryError,
    RepositoryUnavailableError,
    read_head,
)
from app.services.requirement import RequirementService
from app.services.retrieval import RetrievalService
from app.tools.registry import ToolInputError as ToolInputError  # re-exported for callers

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
        result["content"] = _select_sections(content, valid, sections, report, flag)
    result["truncated_sections"] = report
    result["truncated"] = flag[0] or bool(report)
    return result


def _select_sections(
    content: dict[str, Any],
    valid: list[str],
    sections: list[str],
    report: dict[str, dict[str, int]],
    flag: list[bool],
) -> dict[str, Any]:
    """Requested sections in canonical order, within the per-section and total size budgets."""
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
    return selected


def get_plan(
    ctx: ToolContext,
    project_id: str,
    version: int | None = None,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        detail = PlanService(
            PlanRepository(session),
            ArchitectureRepository(session),
            GraphRepository(session),
            ProjectRepository(session),
            RequirementRepository(session),
        ).get_version(pid, version)
        if detail is None:
            raise ToolInputError(
                "The project has no approved implementation plan"
                if version is None
                else f"The project has no implementation plan version {version}"
            )
        plan = detail.plan
        derived = {
            task_id: task_status(found)
            for task_id, found in PatchRepository(session).statuses_by_task(pid, plan.id).items()
        }
        tasks_json = []
        for t in detail.tasks:
            item = t.as_json()
            item["status"] = derived.get(t.id, "todo")
            for dep in item["dependencies"]:
                edit = detail.manual_reason(t, dep["key"])
                if edit is not None:
                    dep.update(reason=edit.reason, author=edit.author)
            tasks_json.append(item)
        content: dict[str, Any] = {
            "tasks": tasks_json,
            "manual_edits": [e.as_json() for e in detail.edits],
            "unattached_questions": list(plan.unattached_questions),
            "not_for_client": list(plan.not_for_client),
        }
        valid = list(content)
        unknown = [s for s in sections or [] if s not in content]
        if unknown:
            raise ToolInputError(
                f"Unknown section(s): {', '.join(unknown)}. Valid sections: {', '.join(valid)}"
            )
        result: dict[str, Any] = {
            "project_id": str(pid),
            "plan_id": str(plan.id),
            "proposal_id": str(plan.proposal_id),
            "version": plan.version,
            "status": plan.status,
            "approval_status": plan.status,
            "decisions": {
                "label": BLOCK_LABEL,
                "items": [d.as_json() for d in DecisionRepository(session).current(pid)],
            },
            "dependency_count": plan.dependency_count,
            "unmapped_node_count": plan.unmapped_node_count,
            "warnings": list(plan.warnings),
            "previous_version": detail.previous_version,
            "previous_manual_edits": [e.as_json() for e in detail.previous_edits],
            "generated_at": plan.generated_at.isoformat(),
            "source_graph_built_at": (
                plan.source_graph_built_at.isoformat() if plan.source_graph_built_at else None
            ),
        }
    report: dict[str, dict[str, int]] = {}
    flag = [False]
    if not sections:
        result["sections_index"] = {name: {"items": len(v)} for name, v in content.items()}
        result["hint"] = "Pass sections=[...] to get_plan to fetch section content."
    else:
        result["content"] = _select_sections(content, valid, sections, report, flag)
    result["truncated_sections"] = report
    result["truncated"] = flag[0] or bool(report)
    return result


MAX_PATCH_FILES = 20
MAX_PATCH_FILE_CHARS = 20_000
MAX_TREE_ENTRIES = 500
MAX_DIFF_CHARS = 20_000


def get_patch(
    ctx: ToolContext, project_id: str, task_key: str, version: int | None = None
) -> dict[str, Any]:
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        plans = PlanService(
            PlanRepository(session),
            ArchitectureRepository(session),
            GraphRepository(session),
            ProjectRepository(session),
            RequirementRepository(session),
        )
        detail = plans.get_version(pid, None)
        if detail is None:
            raise ToolInputError("The project has no approved implementation plan")
        task = next((t for t in detail.tasks if t.key == task_key), None)
        if task is None:
            raise ToolInputError(f"Unknown task_key {task_key}")
        repo = PatchRepository(session)
        patches = sorted(repo.for_task(pid, task.id), key=lambda p: p.version)
        if not patches:
            raise ToolInputError(f"Task {task_key} has no patch yet")
        if version is None:
            accepted = [p for p in patches if p.status == "accepted"]
            patch = (accepted or patches)[-1]
        else:
            found = [p for p in patches if p.version == version]
            if not found:
                raise ToolInputError(f"Task {task_key} has no patch version {version}")
            patch = found[0]
        files = list(repo.files(pid, patch.id))
        report: dict[str, dict[str, int]] = {}
        flag = [False]
        shown = []
        for f in files[:MAX_PATCH_FILES]:
            content, cut = _truncate(f.content, MAX_PATCH_FILE_CHARS)
            if cut:
                report[f"files[{f.path}].content"] = {
                    "returned": len(content),
                    "total": len(f.content),
                }
            shown.append(
                {
                    "path": f.path,
                    "change": f.change,
                    "content": content,
                    "previous_content_hash": f.previous_content_hash,
                }
            )
        if len(files) > MAX_PATCH_FILES:
            report["files"] = {"returned": MAX_PATCH_FILES, "total": len(files)}
        sections = {
            "notes": patch.notes,
            "assumptions": patch.assumptions,
            "test_suggestions": patch.test_suggestions,
            "warnings": patch.warnings,
        }
        capped = {name: _cap_section(name, value, report, flag) for name, value in sections.items()}
        result: dict[str, Any] = {
            "project_id": str(pid),
            "task_key": task.key,
            "patch_id": str(patch.id),
            "version": patch.version,
            "status": patch.status,
            "versions": [{"version": p.version, "status": p.status} for p in patches],
            "provider": patch.provider,
            "model": patch.model,
            "prompt_version": patch.prompt_version,
            "reviewed_by": patch.reviewed_by,
            "review_reason": patch.review_reason,
            "validation": "passed",
            "files": shown,
            **capped,
        }
    result["truncated_sections"] = report
    result["truncated"] = flag[0] or bool(report)
    return result


def get_workspace_tree(ctx: ToolContext, project_id: str) -> dict[str, Any]:
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        files = list(PatchRepository(session).workspace(pid))
        entries = [
            {
                "path": f.path,
                "size": len(f.content.encode("utf-8")),
                "content_hash": f.content_hash,
                "source_patch_id": str(f.source_patch_id),
            }
            for f in files[:MAX_TREE_ENTRIES]
        ]
        total = len(files)
    report: dict[str, dict[str, int]] = {}
    if total > MAX_TREE_ENTRIES:
        report["files"] = {"returned": MAX_TREE_ENTRIES, "total": total}
    return {
        "project_id": str(pid),
        "total": total,
        "files": entries,
        "truncated_sections": report,
        "truncated": bool(report),
    }


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


def graph_related(
    ctx: ToolContext,
    project_id: str,
    source_ref: str,
    requirement_ids: list[str],
    *,
    per_root: int = 8,
) -> dict[str, Any]:
    """Direct impact of the item a task is about and of its linked requirements (agent context).

    Run-scoped: the project, the item and the requirements come from the run, never from the
    model. Bounded by `per_root` hits per root."""
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        try:
            proposal_id = _approved_graph(session, pid)
        except ToolInputError as exc:
            return {"available": False, "reason": str(exc), "roots": []}
        service = _graph_service(session)
        nodes = service.get(pid, proposal_id).nodes
        wanted = set(requirement_ids)
        roots = [
            n
            for n in sorted(nodes, key=lambda n: (n.kind, n.source_ref))
            if (n.source_ref == source_ref and n.kind in ("component", "decision"))
            or (n.kind == "requirement" and n.source_ref in wanted)
        ]
        found = []
        for root in roots:
            view = service.impact(pid, proposal_id, root.id, mode=ImpactMode.DIRECT)
            hits = [h for h in view.result.hits if h.node_id in view.nodes]
            found.append(
                {
                    **_node(root),
                    "affected": [
                        {**_node(view.nodes[h.node_id]), "via": h.via_kind.value}
                        for h in hits[:per_root]
                    ],
                    "total": len(hits),
                }
            )
        return {"available": True, "reason": None, "roots": found}


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


def _hit_json(h: Any) -> tuple[dict[str, Any], bool]:
    text, cut = _truncate(h.text, MAX_STRING_CHARS)
    return {
        "document_id": str(h.document_id),
        "filename": h.filename,
        "chunk_index": h.chunk_index,
        "source_start": h.source_start,
        "source_end": h.source_end,
        "score": h.score,
        "matched_terms": list(h.matched_terms),
        "text": text,
    }, cut


def search_documents_auto(
    ctx: ToolContext,
    project_id: str,
    query: str,
    top_k: int,
    embedder: EmbeddingProvider | None,
) -> dict[str, Any]:
    """Mode auto: hybrid only with a real embedding provider and an embedded project, else BM25.

    The fallback is never silent: `note` says which mode ran and why. A test (fake) provider is
    never used for real searches."""
    settings = get_settings()
    top_k = max(1, min(top_k, MAX_TOP_K))
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        retrieval = RetrievalService(
            ChunkRepository(session),
            DocumentRepository(session),
            ProjectRepository(session),
            ChunkSettings(settings.retrieval_chunk_size, settings.retrieval_chunk_overlap),
            settings.retrieval_max_scan_chunks,
        )
        embeddings = EmbeddingRepository(session)
        reason: str | None = None
        if embedder is None:
            reason = "no embedding provider is configured"
        elif embedder.is_test_provider:
            reason = "the embedding provider is a test provider"
        elif not embeddings.scan(pid, None, space_of(embedder), 1):
            reason = "this project has no embeddings yet"
        try:
            if reason is None:
                outcome = HybridRetrievalService(
                    retrieval, embeddings, embedder, settings.retrieval_max_scan_chunks
                ).search(pid, query, mode="hybrid", top_k=top_k)
                ranked = outcome.hits
                mode = "hybrid" if outcome.effective_mode == "hybrid" else "bm25"
                notes = list(outcome.warnings)
                if mode == "bm25":
                    notes.insert(0, "Hybrid search was unavailable; BM25 results only.")
                scanned, unindexed, limited = (
                    outcome.scanned_chunks,
                    outcome.unindexed_documents,
                    outcome.scan_limited,
                )
                hits_in = [(r.hit, r.score) for r in ranked]
            else:
                plain = retrieval.search(pid, query, top_k=top_k)
                mode, notes = "bm25", [f"BM25 results only: {reason}."]
                scanned, unindexed, limited = (
                    plain.scanned_chunks,
                    plain.unindexed_documents,
                    plain.scan_limited,
                )
                hits_in = [(h, h.score) for h in plain.hits]
        except InvalidQueryError as exc:
            raise ToolInputError(str(exc)) from None
    cut_text = False
    hits = []
    for hit, score in hits_in:
        item, cut = _hit_json(hit)
        item["score"] = score
        cut_text = cut_text or cut
        hits.append(item)
    return {
        "project_id": str(pid),
        "mode": mode,
        "note": " ".join(notes) if notes else None,
        "hits": hits,
        "scanned_chunks": scanned,
        "unindexed_documents": unindexed,
        "truncated": limited or cut_text,
    }


# ---- repository code map ---------------------------------------------------------------------


@contextmanager
def _repository(ctx: ToolContext, project_id: str) -> Iterator[tuple[RepoMap, Path, str | None]]:
    """The project's code map (scanned once when missing), its clone and the current HEAD."""
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        service = RepoMapService(session, get_settings().workspace_root)
        try:
            repo_map = service.ensure(pid)
            _, clone = service.clone(pid)
        except RepositoryUnavailableError as exc:
            raise ToolInputError(str(exc)) from None
        except OSError:
            logger.exception("repository map failed project=%s", pid)
            raise ToolInputError("The repository map could not be built") from None
    _, head = read_head(clone)
    try:
        yield repo_map, clone, head
    except RepoQueryError as exc:
        raise ToolInputError(str(exc)) from None


def get_repo_summary(ctx: ToolContext, project_id: str) -> dict[str, Any]:
    with _repository(ctx, project_id) as (repo_map, _clone, head):
        return repo_queries.summary_view(repo_map, head)


def find_symbol(
    ctx: ToolContext, project_id: str, name: str, kind: str | None = None
) -> dict[str, Any]:
    with _repository(ctx, project_id) as (repo_map, _clone, _head):
        return repo_queries.find_symbol(repo_map, name, kind)


def find_references(ctx: ToolContext, project_id: str, name: str) -> dict[str, Any]:
    with _repository(ctx, project_id) as (repo_map, clone, _head):
        return repo_queries.find_references(repo_map, clone, name)


def get_file_dependants(
    ctx: ToolContext, project_id: str, path: str, transitive: bool = False
) -> dict[str, Any]:
    with _repository(ctx, project_id) as (repo_map, _clone, _head):
        return repo_queries.file_dependants(repo_map, path, transitive)


def search_repository(
    ctx: ToolContext, project_id: str, query: str, prefix: str = ""
) -> dict[str, Any]:
    with _repository(ctx, project_id) as (repo_map, clone, _head):
        return repo_queries.search(repo_map, clone, query, prefix or None)


def repository_context(ctx: ToolContext, project_id: str, task_text: str) -> dict[str, Any] | None:
    """The bounded repository block of an agent brief; None when no repository is connected."""
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        service = RepoMapService(session, get_settings().workspace_root)
        try:
            if service.settings(pid) is None:
                return None
            repo_map = service.ensure(pid)
            _, clone = service.clone(pid)
        except RepositoryUnavailableError as exc:
            return {"note": repo_queries.CONTEXT_NOTE, "available": False, "reason": str(exc)}
        except OSError:
            logger.exception("repository map failed project=%s", pid)
            return {
                "note": repo_queries.CONTEXT_NOTE,
                "available": False,
                "reason": "The repository map could not be built",
            }
    branch, head = read_head(clone)
    return repo_queries.agent_context(repo_map, branch, head, task_text)


REPOSITORY_FILE_SYMBOLS = 30


def repository_files(ctx: ToolContext, project_id: str) -> dict[str, Any] | None:
    """Every file of the code map, for checklist proposals; None when no repository is connected.

    `files` maps source and test files to their first symbol names; `other_paths` lists the rest
    (configuration, documentation) so a revision may name them without inventing a path.
    """
    with read_only_session(ctx) as session:
        pid = _project(session, project_id)
        service = RepoMapService(session, get_settings().workspace_root)
        try:
            if service.settings(pid) is None:
                return None
            repo_map = service.ensure(pid)
        except RepositoryUnavailableError as exc:
            return {"available": False, "reason": str(exc)}
        except OSError:
            logger.exception("repository map failed project=%s", pid)
            return {"available": False, "reason": "The repository map could not be built"}
    files: dict[str, list[str]] = {}
    other: list[str] = []
    for path, info in sorted(repo_map.files.items()):
        if info.get("kind") in ("source", "test"):
            files[path] = [s[0] for s in info.get("symbols", [])[:REPOSITORY_FILE_SYMBOLS]]
        else:
            other.append(path)
    return {
        "available": True,
        "files": files,
        "other_paths": other,
        "source_roots": list(repo_map.summary.get("source_roots", []))[:8],
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
