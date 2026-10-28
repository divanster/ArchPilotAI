import logging
import time
from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import get_database, make_session_factory
from app.domain.chunking import ChunkSettings
from app.domain.patch import PatchLimits
from app.embeddings.contracts import EmbeddingProvider
from app.embeddings.factory import create_embedding_provider
from app.llm.contracts import LLMProvider, LLMRequest, LLMResponse
from app.llm.exceptions import LLMConfigurationError
from app.llm.factory import create_provider
from app.mcp.tools import ToolContext
from app.repositories.agent import AgentRepository
from app.repositories.architecture import ArchitectureRepository
from app.repositories.chunk import ChunkRepository
from app.repositories.document import DocumentRepository
from app.repositories.embedding import EmbeddingRepository
from app.repositories.graph import GraphRepository
from app.repositories.patch import PatchRepository
from app.repositories.plan import PlanRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.agent import AgentConfig, AgentService
from app.services.ai_service import AIService
from app.services.architecture import ArchitectureService
from app.services.architecture_generation import ArchitectureGenerationService
from app.services.architecture_lint import lint_config_from_settings
from app.services.document import DocumentService
from app.services.embedding import EmbeddingService
from app.services.graph import GraphService
from app.services.hybrid import HybridRetrievalService
from app.services.patch import PatchService
from app.services.plan import PlanService
from app.services.project import ProjectService
from app.services.requirement import RequirementService
from app.services.requirement_extraction import RequirementExtractionService
from app.services.retrieval import RetrievalService
from app.services.static_checks import StaticChecker

logger = logging.getLogger(__name__)


def get_engine() -> Engine:
    return get_database().engine


def get_session_factory() -> sessionmaker[Session]:
    return make_session_factory(get_engine())


def get_session() -> Iterator[Session]:
    with get_session_factory()() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_project_service(session: SessionDep) -> ProjectService:
    return ProjectService(ProjectRepository(session))


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]


def get_max_upload_bytes() -> int:
    return get_settings().max_upload_bytes


MaxUploadBytesDep = Annotated[int, Depends(get_max_upload_bytes)]


def get_document_service(session: SessionDep, max_bytes: MaxUploadBytesDep) -> DocumentService:
    return DocumentService(DocumentRepository(session), ProjectRepository(session), max_bytes)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


@lru_cache
def _build_ai_service() -> AIService:
    settings = get_settings()
    return AIService(
        create_provider(settings),
        max_prompt_chars=settings.ai_max_prompt_chars,
        max_output_tokens=settings.ai_max_output_tokens,
        min_interval_seconds=settings.ai_min_interval_seconds,
    )


def get_ai_service() -> AIService:
    try:
        return _build_ai_service()
    except LLMConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.public_message) from exc


AIServiceDep = Annotated[AIService, Depends(get_ai_service)]


@lru_cache
def _build_provider() -> LLMProvider:
    return create_provider(get_settings())


def get_llm_provider() -> LLMProvider:
    """Provider for document extraction. A real provider needs an explicit opt-in."""
    settings = get_settings()
    if settings.llm_provider != "fake" and not settings.extraction_allow_real_provider:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Requirements extraction with a real provider is disabled. "
            "Set ARCHPILOT_EXTRACTION_ALLOW_REAL_PROVIDER=true to opt in.",
        )
    try:
        return _build_provider()
    except LLMConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.public_message) from exc


LLMProviderDep = Annotated[LLMProvider, Depends(get_llm_provider)]


def get_requirement_service(session: SessionDep) -> RequirementService:
    return RequirementService(RequirementRepository(session), ProjectRepository(session))


RequirementServiceDep = Annotated[RequirementService, Depends(get_requirement_service)]


def get_extraction_service(
    session: SessionDep, provider: LLMProviderDep
) -> RequirementExtractionService:
    settings = get_settings()
    return RequirementExtractionService(
        provider,
        DocumentRepository(session),
        ProjectRepository(session),
        RequirementRepository(session),
        max_document_chars=settings.extraction_max_document_chars,
        max_output_tokens=settings.extraction_max_output_tokens,
    )


ExtractionServiceDep = Annotated[RequirementExtractionService, Depends(get_extraction_service)]


def get_graph_service(session: SessionDep) -> GraphService:
    return GraphService(
        GraphRepository(session),
        ArchitectureRepository(session),
        ProjectRepository(session),
        RequirementRepository(session),
        hub_threshold=get_settings().graph_hub_threshold,
    )


GraphServiceDep = Annotated[GraphService, Depends(get_graph_service)]


def get_plan_service(session: SessionDep) -> PlanService:
    return PlanService(
        PlanRepository(session),
        ArchitectureRepository(session),
        GraphRepository(session),
        ProjectRepository(session),
        RequirementRepository(session),
    )


PlanServiceDep = Annotated[PlanService, Depends(get_plan_service)]


def get_patch_repository(session: SessionDep) -> PatchRepository:
    return PatchRepository(session)


PatchRepositoryDep = Annotated[PatchRepository, Depends(get_patch_repository)]


class _NoGeneration:
    """Stands in for the provider on read/review endpoints, which never call the model."""

    provider_name = "none"
    model_name = "none"

    def generate(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("This endpoint does not generate")


def _patch_service(session: Session, provider: LLMProvider) -> PatchService:
    settings = get_settings()
    return PatchService(
        provider,
        ProjectRepository(session),
        PlanRepository(session),
        ArchitectureRepository(session),
        RequirementRepository(session),
        PatchRepository(session),
        limits=PatchLimits(
            settings.patch_max_files,
            settings.patch_max_file_bytes,
            settings.patch_max_patch_bytes,
        ),
        max_output_tokens=settings.patch_max_output_tokens,
        max_context_chars=settings.patch_max_context_chars,
    )


def get_patch_service(session: SessionDep, provider: LLMProviderDep) -> PatchService:
    return _patch_service(session, provider)


def get_patch_review_service(session: SessionDep) -> PatchService:
    return _patch_service(session, _NoGeneration())


def make_agent_service(
    session: Session,
    provider: LLMProvider,
    *,
    clock: Callable[[], float] = time.monotonic,
    checker: StaticChecker | None = None,
) -> AgentService:
    settings = get_settings()
    return AgentService(
        provider,
        _patch_service(session, _NoGeneration()),
        AgentRepository(session),
        PatchRepository(session),
        PlanRepository(session),
        ToolContext(sessionmaker(bind=session.get_bind(), expire_on_commit=False)),
        checker
        or StaticChecker(
            temp_parent=settings.agent_check_temp_dir,
            timeout=settings.agent_check_timeout_seconds,
            tsc_path=settings.agent_tsc_path,
        ),
        limits=PatchLimits(
            settings.patch_max_files,
            settings.patch_max_file_bytes,
            settings.patch_max_patch_bytes,
        ),
        config=AgentConfig(
            max_steps=settings.agent_max_steps,
            token_budget=settings.agent_token_budget,
            time_limit_seconds=settings.agent_time_limit_seconds,
            step_max_output_tokens=settings.agent_step_max_output_tokens,
            context_chars=settings.agent_context_chars,
            last_results=settings.agent_context_last_results,
        ),
        clock=clock,
    )


def get_agent_service(session: SessionDep, provider: LLMProviderDep) -> AgentService:
    return make_agent_service(session, provider)


def get_agent_read_service(session: SessionDep) -> AgentService:
    return make_agent_service(session, _NoGeneration())


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
AgentReadServiceDep = Annotated[AgentService, Depends(get_agent_read_service)]

PatchServiceDep = Annotated[PatchService, Depends(get_patch_service)]
PatchReviewServiceDep = Annotated[PatchService, Depends(get_patch_review_service)]


def get_architecture_service(session: SessionDep) -> ArchitectureService:
    return ArchitectureService(
        ArchitectureRepository(session),
        ProjectRepository(session),
        RequirementRepository(session),
        lint_config=lint_config_from_settings(get_settings()),
        on_approved=get_graph_service(session).build,
    )


ArchitectureServiceDep = Annotated[ArchitectureService, Depends(get_architecture_service)]


def get_architecture_generation_service(
    session: SessionDep, provider: LLMProviderDep
) -> ArchitectureGenerationService:
    settings = get_settings()
    return ArchitectureGenerationService(
        provider,
        ProjectRepository(session),
        DocumentRepository(session),
        RequirementRepository(session),
        ArchitectureRepository(session),
        max_requirements=settings.architecture_max_requirements,
        max_context_chars=settings.architecture_max_context_chars,
        max_unconfirmed_chars=settings.architecture_max_unconfirmed_chars,
        max_output_tokens=settings.architecture_max_output_tokens,
        chunks=ChunkRepository(session),
        scan_limit=settings.retrieval_max_scan_chunks,
        passages_per_requirement=settings.architecture_passages_per_requirement,
        max_passages=settings.architecture_max_passages,
        lint_config=lint_config_from_settings(settings),
    )


ArchitectureGenerationServiceDep = Annotated[
    ArchitectureGenerationService, Depends(get_architecture_generation_service)
]


def get_retrieval_service(session: SessionDep) -> RetrievalService:
    settings = get_settings()
    return RetrievalService(
        ChunkRepository(session),
        DocumentRepository(session),
        ProjectRepository(session),
        ChunkSettings(settings.retrieval_chunk_size, settings.retrieval_chunk_overlap),
        settings.retrieval_max_scan_chunks,
    )


RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]


def get_embedding_provider() -> EmbeddingProvider | None:
    try:
        return create_embedding_provider(get_settings())
    except LLMConfigurationError:
        # Misconfiguration disables vector search (reported as unavailable); never a fake.
        logger.warning("embedding provider is misconfigured; vector search disabled")
        return None


EmbeddingProviderDep = Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)]


def get_embedding_service(session: SessionDep, provider: EmbeddingProviderDep) -> EmbeddingService:
    return EmbeddingService(
        EmbeddingRepository(session),
        DocumentRepository(session),
        ProjectRepository(session),
        provider,
    )


EmbeddingServiceDep = Annotated[EmbeddingService, Depends(get_embedding_service)]


def get_search_service(
    session: SessionDep, retrieval: RetrievalServiceDep, provider: EmbeddingProviderDep
) -> HybridRetrievalService:
    return HybridRetrievalService(
        retrieval,
        EmbeddingRepository(session),
        provider,
        get_settings().retrieval_max_scan_chunks,
    )


SearchServiceDep = Annotated[HybridRetrievalService, Depends(get_search_service)]
