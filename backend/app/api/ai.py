from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AIServiceDep
from app.api.schemas import AIGenerateRequest, AIGenerateResponse
from app.core.config import get_settings
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.services.ai_service import (
    AIBusyError,
    AIThrottledError,
    PromptTooLongError,
)


def require_non_production() -> None:
    # No authentication exists yet, so this endpoint must never be reachable in production.
    if get_settings().environment == "production":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The AI endpoint is disabled in production until authentication is implemented.",
        )


router = APIRouter(prefix="/api/ai", tags=["ai"], dependencies=[Depends(require_non_production)])


def _upstream_error(exc: LLMError) -> HTTPException:
    """Map provider failures to safe responses; upstream bodies are never forwarded."""
    code = status.HTTP_502_BAD_GATEWAY
    if isinstance(exc, LLMRateLimitError):
        code = status.HTTP_429_TOO_MANY_REQUESTS
    elif isinstance(exc, LLMTimeoutError):
        code = status.HTTP_504_GATEWAY_TIMEOUT
    # Upstream 401/403 are reported as 502 so clients do not confuse them with app auth.
    elif isinstance(exc, LLMAuthenticationError):
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(code, exc.public_message)


@router.post("/generate", response_model=AIGenerateResponse)
def generate(body: AIGenerateRequest, service: AIServiceDep) -> AIGenerateResponse:
    try:
        result = service.generate(body.prompt)
    except PromptTooLongError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except AIBusyError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except AIThrottledError as exc:
        retry_after = max(1, int(exc.retry_after_seconds + 0.999))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait before sending another request.",
            headers={"Retry-After": str(retry_after)},
        ) from exc
    except LLMError as exc:
        raise _upstream_error(exc) from exc
    return AIGenerateResponse(
        answer=result.text,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        usage=result.usage,
    )
