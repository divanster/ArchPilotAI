"""Provider status and an explicit, user-triggered connection test. Never returns secrets."""

import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import get_embedding_provider
from app.core.config import get_settings
from app.embeddings.contracts import EmbeddingError, EmbeddingProvider
from app.llm.contracts import LLMProvider, LLMRequest
from app.llm.exceptions import LLMConfigurationError, LLMError
from app.llm.factory import create_provider

router = APIRouter(prefix="/api/settings/providers", tags=["settings"])

ProviderKind = Literal["chat", "embeddings"]
ProviderStatus = Literal["configured", "not configured", "disabled"]


class ProviderInfo(BaseModel):
    kind: ProviderKind
    provider: str
    status: ProviderStatus
    model: str | None = None
    dimension: int | None = None
    detail: str | None = None


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]


class ConnectionTestResponse(BaseModel):
    kind: ProviderKind
    ok: bool
    message: str
    latency_ms: int | None = None


def _chat_provider() -> LLMProvider | None:
    try:
        return create_provider(get_settings())
    except LLMConfigurationError:
        return None


def _embedding_provider() -> EmbeddingProvider | None:
    return get_embedding_provider()


ChatDep = Annotated[LLMProvider | None, Depends(_chat_provider)]
EmbedDep = Annotated[EmbeddingProvider | None, Depends(_embedding_provider)]


def _chat_info(provider: LLMProvider | None) -> ProviderInfo:
    settings = get_settings()
    name = settings.llm_provider
    if provider is None:
        return ProviderInfo(
            kind="chat",
            provider=name,
            status="not configured",
            detail="Required settings are missing or invalid.",
        )
    return ProviderInfo(kind="chat", provider=name, status="configured", model=provider.model_name)


def _embedding_info(provider: EmbeddingProvider | None) -> ProviderInfo:
    settings = get_settings()
    name = settings.embedding_provider
    if name == "none":
        return ProviderInfo(
            kind="embeddings",
            provider=name,
            status="disabled",
            detail="Vector search is off; keyword search is used.",
        )
    if provider is None:
        return ProviderInfo(
            kind="embeddings",
            provider=name,
            status="not configured",
            detail="Required settings are missing or invalid.",
        )
    return ProviderInfo(
        kind="embeddings",
        provider=name,
        status="configured",
        model=provider.model_name,
        dimension=provider.dimension,
        detail="Test provider (not for real search)." if provider.is_test_provider else None,
    )


@router.get("", response_model=ProvidersResponse)
def list_providers(chat: ChatDep, embeddings: EmbedDep) -> ProvidersResponse:
    return ProvidersResponse(providers=[_chat_info(chat), _embedding_info(embeddings)])


@router.post("/{kind}/test", response_model=ConnectionTestResponse)
def test_connection(
    kind: ProviderKind, chat: ChatDep, embeddings: EmbedDep
) -> ConnectionTestResponse:
    """One minimal request, only when called (a user click). Messages carry no secrets or text."""
    started = time.monotonic()

    def done(ok: bool, message: str) -> ConnectionTestResponse:
        ms = int((time.monotonic() - started) * 1000)
        return ConnectionTestResponse(kind=kind, ok=ok, message=message, latency_ms=ms)

    if kind == "chat":
        if chat is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "The chat provider is not configured.")
        try:
            chat.generate(LLMRequest(prompt="ping", max_output_tokens=8, temperature=0))
        except LLMError as exc:
            return done(False, f"Request failed ({type(exc).__name__}).")
        return done(True, "Connection works.")

    info = _embedding_info(embeddings)
    if embeddings is None or info.status != "configured":
        raise HTTPException(status.HTTP_409_CONFLICT, f"The embeddings provider is {info.status}.")
    try:
        result = embeddings.embed(["ping"])
    except EmbeddingError as exc:
        return done(False, f"Request failed ({type(exc).__name__}).")
    expected = get_settings().embedding_dimension or embeddings.dimension
    if len(result.vectors) != 1 or result.dimension != expected:
        return done(
            False, f"Unexpected response: dimension {result.dimension}, expected {expected}."
        )
    return done(True, f"Connection works (dimension {result.dimension}).")
