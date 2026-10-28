import time

from app.llm.architecture_prompt import (
    fake_architecture_answer,
    fake_revision_answer,
    is_architecture_request,
    is_revision_request,
)
from app.llm.contracts import LLMRequest, LLMResponse, TokenUsage
from app.llm.exceptions import LLMError
from app.llm.extraction_prompt import fake_extraction_answer, is_extraction_request
from app.llm.patch_prompt import fake_patch_answer, is_patch_request


class FakeLLMProvider:
    """Deterministic, offline provider for unit and E2E tests. Needs no credentials."""

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        model: str = "fake-model-1",
        error: LLMError | None = None,
        extraction_text: str | None = None,
        architecture_text: str | None = None,
        patch_text: str | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._model = model
        self._error = error
        # Fixed answer for patch prompts; None derives a small valid patch from the context.
        self._patch_text = patch_text
        # Fixed answer for requirements-extraction prompts; None derives one from the document.
        self._extraction_text = extraction_text
        # Fixed answer for architecture prompts; None derives one from the supplied context.
        self._architecture_text = architecture_text
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
        if text is None and is_extraction_request(request.system_prompt):
            text = (
                self._extraction_text
                if self._extraction_text is not None
                else fake_extraction_answer(request.prompt)
            )
        if text is None and is_patch_request(request.system_prompt):
            text = (
                self._patch_text
                if self._patch_text is not None
                else fake_patch_answer(request.prompt)
            )
        if text is None and is_revision_request(request.system_prompt):
            text = (
                self._architecture_text
                if self._architecture_text is not None
                else fake_revision_answer(request.prompt)
            )
        if text is None and is_architecture_request(request.system_prompt):
            text = (
                self._architecture_text
                if self._architecture_text is not None
                else fake_architecture_answer(request.prompt)
            )
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
