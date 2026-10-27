import logging
import threading
import time
import uuid
from collections.abc import Sequence

from app.domain.requirement import (
    PROMPT_VERSION,
    DocumentTooLargeForExtractionError,
    ExtractionError,
    ExtractionStatus,
    RequirementStatus,
    locate,
    parse_output,
)
from app.llm.contracts import LLMProvider, LLMRequest, LLMResponse
from app.llm.exceptions import LLMError
from app.llm.extraction_prompt import SYSTEM_PROMPT, build_prompt, new_nonce
from app.models.requirement import ExtractionRun, Requirement
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.document import DocumentNotFoundError
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)

# One extraction at a time per process: bounds provider cost and makes the
# "skip spans that already exist" check race-free for a local, single-process app.
_extraction_lock = threading.Lock()


class ExtractionBusyError(Exception):
    pass


class ExtractionResult:
    def __init__(
        self, run: ExtractionRun, created: Sequence[Requirement], skipped_duplicates: int
    ) -> None:
        self.run = run
        self.created = created
        self.skipped_duplicates = skipped_duplicates


class RequirementExtractionService:
    def __init__(
        self,
        provider: LLMProvider,
        documents: DocumentRepository,
        projects: ProjectRepository,
        requirements: RequirementRepository,
        *,
        max_document_chars: int,
        max_output_tokens: int,
    ) -> None:
        self._provider = provider
        self._documents = documents
        self._projects = projects
        self._requirements = requirements
        self._max_document_chars = max_document_chars
        self._max_output_tokens = max_output_tokens

    def _run(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        status: ExtractionStatus,
        *,
        error_code: str | None = None,
        response: LLMResponse | None = None,
        latency_ms: int | None = None,
    ) -> ExtractionRun:
        usage = response.usage if response else None
        return ExtractionRun(
            project_id=project_id,
            document_id=document_id,
            provider=response.provider if response else self._provider.provider_name,
            model=response.model if response else self._provider.model_name,
            prompt_version=PROMPT_VERSION,
            status=status.value,
            error_code=error_code,
            latency_ms=response.latency_ms if response else latency_ms,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
        )

    def extract(self, project_id: uuid.UUID, document_id: uuid.UUID) -> ExtractionResult:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        document = self._documents.get(project_id, document_id)
        if document is None:
            raise DocumentNotFoundError(document_id)
        if not _extraction_lock.acquire(blocking=False):
            raise ExtractionBusyError("An extraction is already running")
        try:
            return self._extract(project_id, document_id, document.extracted_text)
        finally:
            _extraction_lock.release()

    def _fail(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        code: str,
        response: LLMResponse | None = None,
        latency_ms: int | None = None,
    ) -> None:
        run = self._run(
            project_id,
            document_id,
            ExtractionStatus.FAILED,
            error_code=code,
            response=response,
            latency_ms=latency_ms,
        )
        self._requirements.record_failed_run(run)
        logger.info("extraction failed document=%s code=%s", document_id, code)

    def _extract(
        self, project_id: uuid.UUID, document_id: uuid.UUID, text: str
    ) -> ExtractionResult:
        if len(text) > self._max_document_chars:
            self._fail(project_id, document_id, DocumentTooLargeForExtractionError.code)
            raise DocumentTooLargeForExtractionError(
                f"Document exceeds the {self._max_document_chars} character extraction limit"
            )

        nonce = new_nonce()
        while nonce in text:
            nonce = new_nonce()
        request = LLMRequest(
            prompt=build_prompt(text, nonce),
            system_prompt=SYSTEM_PROMPT,
            max_output_tokens=self._max_output_tokens,
            temperature=0,
        )
        started = time.perf_counter()
        try:
            response = self._provider.generate(request)
        except LLMError:
            elapsed = int((time.perf_counter() - started) * 1000)
            self._fail(project_id, document_id, "provider_error", latency_ms=elapsed)
            raise

        try:
            located = [locate(text, d) for d in parse_output(response.text).requirements]
        except ExtractionError as exc:
            # Any unsupported or malformed item rejects the whole run; nothing is repaired.
            self._fail(project_id, document_id, exc.code, response=response)
            raise

        claimed = self._requirements.existing_spans(document_id)
        fresh: list[Requirement] = []
        skipped = 0
        for item in located:
            key = (item.source_start, item.source_end, item.draft.type.value)
            if key in claimed:
                skipped += 1
                continue
            claimed.add(key)
            fresh.append(
                Requirement(
                    project_id=project_id,
                    document_id=document_id,
                    type=item.draft.type.value,
                    title=item.draft.title,
                    description=item.draft.description,
                    source_quote=item.draft.source_quote,
                    source_start=item.source_start,
                    source_end=item.source_end,
                    status=RequirementStatus.PROPOSED.value,
                )
            )

        run = self._run(project_id, document_id, ExtractionStatus.SUCCEEDED, response=response)
        run.id = uuid.uuid4()
        for requirement in fresh:
            requirement.extraction_run_id = run.id
        self._requirements.persist_extraction(run, fresh)
        logger.info(
            "extraction succeeded document=%s created=%d skipped=%d",
            document_id,
            len(fresh),
            skipped,
        )
        return ExtractionResult(run, fresh, skipped)
