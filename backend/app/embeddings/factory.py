from app.core.config import Settings
from app.embeddings.contracts import EmbeddingProvider
from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.workbench import WorkbenchEmbeddingProvider
from app.llm.exceptions import LLMConfigurationError
from app.llm.factory import require_workbench_settings
from app.llm.workbench_provider import WorkbenchConfig


def workbench_config(settings: Settings) -> WorkbenchConfig:
    return WorkbenchConfig(
        endpoint=settings.workbench_endpoint.strip(),
        deployment=settings.embedding_deployment.strip(),
        api_version=settings.workbench_api_version.strip(),
        region=settings.workbench_region.strip(),
        charge_code=settings.workbench_charge_code.strip(),
        api_key=settings.workbench_api_key,
        proxy=settings.workbench_proxy.strip() or None,
        timeout=settings.workbench_timeout,
        max_retries=settings.workbench_max_retries,
    )


def create_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """None means vector search is unavailable; keyword search is unaffected.

    A misconfigured Workbench provider raises; it is never replaced by the fake.
    """
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider()
    if settings.embedding_provider == "kpmg_workbench":
        missing = []
        if not settings.embedding_deployment.strip():
            missing.append("ARCHPILOT_EMBEDDING_DEPLOYMENT")
        if settings.embedding_dimension < 1:
            missing.append("ARCHPILOT_EMBEDDING_DIMENSION")
        if missing:
            raise LLMConfigurationError(
                "kpmg_workbench embeddings need: " + ", ".join(sorted(missing))
            )
        require_workbench_settings(settings, need_deployment=False)
        return WorkbenchEmbeddingProvider(
            workbench_config(settings),
            dimension=settings.embedding_dimension,
            batch_size=settings.embedding_batch_size,
        )
    return None
