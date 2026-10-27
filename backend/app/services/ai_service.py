import logging
import threading
import time
from collections.abc import Callable

from app.llm.contracts import LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 16_000


class AIRequestError(Exception):
    """Rejected before any provider call."""


class PromptTooLongError(AIRequestError):
    pass


class AIBusyError(AIRequestError):
    """Another generation is already in flight."""


class AIThrottledError(AIRequestError):
    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Too many requests")


class AIService:
    """Runs one generation at a time, spaced apart, so a local prototype cannot run up costs.

    The guards are per process. Prompts and answers are never logged.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_prompt_chars: int,
        max_output_tokens: int,
        min_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._max_prompt_chars = max_prompt_chars
        self._max_output_tokens = max_output_tokens
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._state = threading.Lock()
        self._in_flight = False
        self._last_started: float | None = None

    def _admit(self) -> None:
        with self._state:
            if self._in_flight:
                raise AIBusyError("A generation is already running")
            now = self._clock()
            if self._last_started is not None:
                remaining = self._min_interval - (now - self._last_started)
                if remaining > 0:
                    raise AIThrottledError(remaining)
            self._in_flight = True
            self._last_started = now

    def generate(self, prompt: str) -> LLMResponse:
        if len(prompt) > self._max_prompt_chars:
            raise PromptTooLongError(f"Prompt exceeds {self._max_prompt_chars} characters")
        self._admit()
        try:
            response = self._provider.generate(
                LLMRequest(prompt=prompt, max_output_tokens=self._max_output_tokens)
            )
        finally:
            with self._state:
                self._in_flight = False
        logger.info(
            "ai generation provider=%s model=%s latency_ms=%d",
            response.provider,
            response.model,
            response.latency_ms,
        )
        return response.model_copy(update={"text": response.text[:MAX_ANSWER_CHARS]})
