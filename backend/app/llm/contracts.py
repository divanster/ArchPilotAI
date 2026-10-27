from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class TokenUsage(BaseModel):
    """Token counts exactly as reported by the provider; None when not reported."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt: str = Field(min_length=1)
    system_prompt: str | None = None
    max_output_tokens: int = Field(default=400, ge=1)
    temperature: float = Field(default=0.2, ge=0, le=2)


class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str
    latency_ms: int = Field(ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    http_status: int | None = None  # None for providers without HTTP
    finish_reason: str | None = None  # as reported by the provider (e.g. "stop", "length")


@runtime_checkable
class LLMProvider(Protocol):
    """All LLM access goes through this. Implementations raise `app.llm.exceptions.LLMError`."""

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate(self, request: LLMRequest) -> LLMResponse: ...
