import json
import logging
import threading
import time
import uuid
from typing import Any

from app.domain.architecture import (
    PROMPT_VERSION,
    RESPONSE_CHARS_PER_TOKEN,
    ArchitectureError,
    ContextTooLargeError,
    NoConfirmedRequirementsError,
    ProposalStatus,
    ProviderMetadata,
    build_alias_map,
    content_to_json,
    parse_generated,
    safe_finish_reason,
    verify_references,
)
from app.llm.architecture_prompt import SYSTEM_PROMPT, build_prompt, new_nonce
from app.llm.contracts import LLMProvider, LLMRequest
from app.models.architecture import ArchitectureProposal
from app.repositories.architecture import ArchitectureRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
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
    ) -> None:
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

    def _build_context(self, project_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, uuid.UUID]]:
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
        if documents and budget > 200:
            share = budget // len(documents) - 120  # leave room for the JSON wrapper
            for document in documents:
                if share <= 0:
                    break
                context["unconfirmed_context"].append(
                    {
                        "status": "unconfirmed",
                        "document": document.original_filename,
                        "excerpt": document.extracted_text[:share],
                    }
                )
        return context, aliases

    def _generate(self, project_id: uuid.UUID) -> ArchitectureProposal:
        context, aliases = self._build_context(project_id)
        nonce = new_nonce()
        while nonce in json.dumps(context):
            nonce = new_nonce()
        request = LLMRequest(
            prompt=build_prompt(context, nonce),
            system_prompt=SYSTEM_PROMPT,
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
            prompt_version=PROMPT_VERSION,
            latency_ms=response.latency_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        saved = self._architecture.add(proposal)
        logger.info("architecture saved project=%s version=%d", project_id, saved.version)
        return saved
