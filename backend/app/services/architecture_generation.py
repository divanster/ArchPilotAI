import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from app.domain.architecture import (
    PROMPT_VERSION,
    RESPONSE_CHARS_PER_TOKEN,
    REVISION_PROMPT_VERSION,
    ArchitectureContent,
    ArchitectureError,
    ContextTooLargeError,
    NoConfirmedRequirementsError,
    PassageCatalog,
    PassageSource,
    ProposalStatus,
    ProviderMetadata,
    build_alias_map,
    content_to_json,
    parse_generated,
    referenced_requirement_ids,
    safe_finish_reason,
    verify_references,
)
from app.domain.revision import draft_for_revision, findings_for_revision
from app.domain.semantic_lint import LintConfig
from app.llm.architecture_prompt import (
    REVISION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_prompt,
    build_revision_prompt,
    new_nonce,
)
from app.llm.contracts import LLMProvider, LLMRequest
from app.models.architecture import ArchitectureProposal
from app.repositories.architecture import ArchitectureRepository
from app.repositories.chunk import ChunkRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture import (
    NothingToReviseError,
    ProposalNotEditableError,
    ProposalNotFoundError,
    StaleProposalError,
)
from app.services.architecture_context import ArchitectureContextRetriever, RequirementQuery
from app.services.architecture_lint import compute_lint
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)

# One generation at a time per process: bounds provider cost and keeps version numbers dense.
_generation_lock = threading.Lock()


class ArchitectureBusyError(Exception):
    pass


class ArchitectureGenerationService:
    def __init__(
        self,
        provider: LLMProvider,
        projects: ProjectRepository,
        documents: DocumentRepository,
        requirements: RequirementRepository,
        architecture: ArchitectureRepository,
        *,
        max_requirements: int,
        max_context_chars: int,
        max_unconfirmed_chars: int,
        max_output_tokens: int,
        chunks: ChunkRepository | None = None,
        scan_limit: int = 20_000,
        passages_per_requirement: int = 3,
        max_passages: int = 12,
        lint_config: LintConfig | None = None,
    ) -> None:
        self._lint_config = lint_config
        self._retriever = ArchitectureContextRetriever(
            chunks,
            scan_limit=scan_limit,
            per_requirement=passages_per_requirement,
            max_passages=max_passages,
        )
        self._provider = provider
        self._projects = projects
        self._documents = documents
        self._requirements = requirements
        self._architecture = architecture
        self._max_requirements = max_requirements
        self._max_context_chars = max_context_chars
        self._max_unconfirmed_chars = max_unconfirmed_chars
        self._max_output_tokens = max_output_tokens

    def generate(self, project_id: uuid.UUID) -> ArchitectureProposal:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        if not _generation_lock.acquire(blocking=False):
            raise ArchitectureBusyError("An architecture generation is already running")
        try:
            return self._generate(project_id)
        finally:
            _generation_lock.release()

    def _build_context(
        self, project_id: uuid.UUID
    ) -> tuple[dict[str, Any], dict[str, uuid.UUID], PassageCatalog]:
        project = self._projects.get(project_id)
        assert project is not None
        confirmed = self._requirements.list(project_id, "confirmed")
        if not confirmed:
            raise NoConfirmedRequirementsError("The project has no confirmed requirements")
        if len(confirmed) > self._max_requirements:
            raise ContextTooLargeError(
                f"The project has more than {self._max_requirements} confirmed requirements"
            )
        # The alias map lives only in this request; the model never sees requirement UUIDs.
        aliases = build_alias_map(r.id for r in confirmed)
        alias_of = {rid: alias for alias, rid in aliases.items()}
        confirmed = sorted(confirmed, key=lambda r: alias_of[r.id])
        documents = list(self._documents.list(project_id))
        names = {d.id: d.original_filename for d in documents}
        context: dict[str, Any] = {
            "project": {"name": project.name, "description": project.description},
            "confirmed_requirements": [
                {
                    "id": alias_of[r.id],
                    "type": r.type,
                    "title": r.title,
                    "description": r.description,
                    "source": {
                        "document": names.get(r.document_id, "unknown"),
                        "quote": r.source_quote,
                        "start": r.source_start,
                        "end": r.source_end,
                    },
                }
                for r in confirmed
            ],
            "unconfirmed_context": [],
        }
        used = len(json.dumps(context, ensure_ascii=False))
        if used > self._max_context_chars:
            raise ContextTooLargeError("The confirmed requirements exceed the context limit")
        budget = min(self._max_unconfirmed_chars, self._max_context_chars - used)
        retrieved = self._retriever.retrieve(
            project_id,
            [RequirementQuery(alias_of[r.id], r.title, r.description) for r in confirmed],
            has_documents=bool(documents),
            budget_chars=budget,
        )
        # Passage aliases are a namespace of their own and are only valid for this request.
        sources: dict[str, PassageSource] = {}
        for number, row in enumerate(retrieved.passages, start=1):
            alias = f"P{number:03d}"
            sources[alias] = PassageSource(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                filename=row.filename,
                source_start=row.source_start,
                source_end=row.source_end,
            )
            context["unconfirmed_context"].append(
                {
                    "id": alias,
                    "status": "unconfirmed",
                    "document": row.filename,
                    "excerpt": row.text,
                }
            )
        return context, aliases, PassageCatalog(sources, retrieved.info)

    def _generate(self, project_id: uuid.UUID) -> ArchitectureProposal:
        context, aliases, catalog = self._build_context(project_id)
        return self._complete(
            project_id,
            context,
            aliases,
            catalog,
            system_prompt=SYSTEM_PROMPT,
            build=build_prompt,
            prompt_version=PROMPT_VERSION,
        )

    def revise(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> ArchitectureProposal:
        """One model call that revises only the flagged items, saved as a new draft version.

        The source draft is read, never modified, and nothing here approves or loops.
        """
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        source = self._architecture.get(project_id, proposal_id)
        if source is None:
            raise ProposalNotFoundError(proposal_id)
        if source.status != ProposalStatus.DRAFT.value:
            raise ProposalNotEditableError(
                f"Only a draft proposal can be revised; this one is {source.status}."
            )
        findings = findings_for_revision(source.lint_findings)
        if not findings:
            raise NothingToReviseError("This draft has no review findings to revise")
        if not _generation_lock.acquire(blocking=False):
            raise ArchitectureBusyError("An architecture generation is already running")
        try:
            context, aliases, catalog = self._build_context(project_id)
            content = ArchitectureContent.model_validate(source.content)
            confirmed = set(aliases.values())
            if referenced_requirement_ids(content) - confirmed:
                raise StaleProposalError(
                    "The draft cites requirements that are no longer confirmed; "
                    "generate a new version instead."
                )
            by_requirement = {str(rid): alias for alias, rid in aliases.items()}
            by_passage = {str(s.chunk_id): alias for alias, s in catalog.sources.items()}
            context["draft"] = draft_for_revision(
                source.title, source.summary, source.content, by_requirement, by_passage
            )
            context["findings"] = findings
            return self._complete(
                project_id,
                context,
                aliases,
                catalog,
                system_prompt=REVISION_SYSTEM_PROMPT,
                build=build_revision_prompt,
                prompt_version=REVISION_PROMPT_VERSION,
                revised_from=(source.id, source.version, len(findings)),
            )
        finally:
            _generation_lock.release()

    def _complete(
        self,
        project_id: uuid.UUID,
        context: dict[str, Any],
        aliases: dict[str, uuid.UUID],
        catalog: PassageCatalog,
        *,
        system_prompt: str,
        build: Callable[[dict[str, Any], str], str],
        prompt_version: str,
        revised_from: tuple[uuid.UUID, int, int] | None = None,
    ) -> ArchitectureProposal:
        nonce = new_nonce()
        while nonce in json.dumps(context):
            nonce = new_nonce()
        request = LLMRequest(
            prompt=build(context, nonce),
            system_prompt=system_prompt,
            max_output_tokens=self._max_output_tokens,
            temperature=0,
        )
        started = time.perf_counter()
        response = self._provider.generate(request)
        metadata = ProviderMetadata(
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            finish_reason=safe_finish_reason(response.finish_reason),
        )
        logger.info(
            "architecture response project=%s latency_ms=%d finish_reason=%s",
            project_id,
            int((time.perf_counter() - started) * 1000),
            metadata.finish_reason,
        )

        try:
            generated = parse_generated(
                response.text,
                finish_reason=metadata.finish_reason,
                max_chars=self._max_output_tokens * RESPONSE_CHARS_PER_TOKEN,
                aliases=aliases,
                passages=catalog,
            )
            verify_references(generated, set(aliases.values()))
        except ArchitectureError as exc:
            exc.metadata = metadata
            # Only fixed labels, counts, schema paths and provider numbers: never content.
            logger.warning(
                "architecture validation failed project=%s code=%s category=%s "
                "diagnostics=%s provider_metadata=%s",
                project_id,
                exc.code,
                exc.category.value if exc.category else None,
                json.dumps(exc.diagnostics, sort_keys=True),
                json.dumps(metadata.as_dict(), sort_keys=True),
            )
            raise

        proposal = ArchitectureProposal(
            project_id=project_id,
            title=generated.title,
            summary=generated.summary,
            status=ProposalStatus.DRAFT.value,
            version=self._architecture.next_version(project_id),
            content=content_to_json(generated),
            provider=response.provider,
            model=response.model,
            prompt_version=prompt_version,
            revised_from_id=revised_from[0] if revised_from else None,
            revised_from_version=revised_from[1] if revised_from else None,
            revised_from_findings=revised_from[2] if revised_from else None,
            latency_ms=response.latency_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            lint_findings=compute_lint(
                self._requirements,
                project_id,
                generated.title,
                generated.summary,
                generated,
                self._lint_config,
            ),
        )
        saved = self._architecture.add(proposal)
        logger.info("architecture saved project=%s version=%d", project_id, saved.version)
        return saved
