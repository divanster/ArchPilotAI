import time

from app.llm.contracts import LLMRequest, LLMResponse, TokenUsage
from app.llm.exceptions import LLMError


class FakeLLMProvider:
    """Deterministic, offline provider for unit and E2E tests. Needs no credentials."""

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        model: str = "fake-model-1",
        error: LLMError | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._model = model
        self._error = error
        self.requests: list[LLMRequest] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        text = self._responses.get(request.prompt)
        if text is None:
            text = f"Fake answer to: {request.prompt[:100]}"
        input_tokens = len(request.prompt.split())
        output_tokens = len(text.split())
        return LLMResponse(
            text=text,
            provider=self.provider_name,
            model=self._model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
