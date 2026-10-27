import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from app.llm.contracts import LLMRequest, LLMResponse, TokenUsage
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMRequestRejectedError,
    LLMTimeoutError,
    LLMUnavailableError,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "kpmg_workbench"
# Documented Chat Completions route; {deployment} is URL-quoted, the api-version is a query param.
CHAT_PATH = "/genai/azure/openai/deployments/{deployment}/chat/completions"
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 30.0
BACKOFF_BASE_SECONDS = 0.5


class WorkbenchConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: str
    deployment: str
    api_version: str
    region: str
    charge_code: str
    api_key: SecretStr
    proxy: str | None = None
    timeout: float = 90.0
    max_retries: int = 2


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "")
    if raw.isdigit():
        return min(float(raw), MAX_RETRY_AFTER_SECONDS)
    return None


# Finish reasons are short fixed words ("stop", "length", ...); anything else is dropped.
_FINISH_REASON = re.compile(r"^[a-z_]{1,32}$")


def _count(value: object) -> int | None:
    # Only genuine, non-negative integers reported by the provider; never estimated.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


class WorkbenchProvider:
    """KPMG Workbench Azure-OpenAI Chat Completions over httpx (subscription-key auth).

    TLS verification is always on. Prompts, responses, headers and upstream error bodies are
    never logged or surfaced.
    """

    def __init__(
        self,
        config: WorkbenchConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._transport = transport
        self._sleep = sleep

    @property
    def provider_name(self) -> str:
        return PROVIDER_NAME

    @property
    def model_name(self) -> str:
        return self._config.deployment

    def __repr__(self) -> str:
        return f"WorkbenchProvider(deployment={self._config.deployment!r})"

    def _client(self) -> httpx.Client:
        timeout = httpx.Timeout(self._config.timeout)
        if self._transport is not None:
            return httpx.Client(transport=self._transport, timeout=timeout)
        return httpx.Client(proxy=self._config.proxy or None, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Ocp-Apim-Subscription-Key": self._config.api_key.get_secret_value(),
            "x-kpmg-charge-code": self._config.charge_code,
            "x-kpmg-region-override": self._config.region,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self) -> str:
        path = CHAT_PATH.format(deployment=quote(self._config.deployment, safe=""))
        return self._config.endpoint.rstrip("/") + path

    @staticmethod
    def _body(request: LLMRequest) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})
        return {
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }

    def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        attempts = self._config.max_retries + 1
        last_error: LLMError | None = None
        wait: float | None = None

        with self._client() as client:
            for attempt in range(attempts):
                if attempt > 0:
                    delay = wait if wait is not None else BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
                    self._sleep(delay)
                wait = None
                try:
                    response = client.post(
                        self._url(),
                        params={"api-version": self._config.api_version},
                        headers=self._headers(),
                        json=self._body(request),
                    )
                except httpx.TimeoutException:
                    last_error = LLMTimeoutError()
                except httpx.TransportError as exc:
                    # Only the exception type is logged: messages can echo URLs or proxies.
                    logger.warning("workbench transport error type=%s", type(exc).__name__)
                    last_error = LLMUnavailableError()
                else:
                    if response.is_success:
                        return self._parse(response, started)
                    last_error, retryable = self._classify(response)
                    wait = _retry_after(response)
                    if not retryable:
                        logger.warning("workbench request failed status=%s", response.status_code)
                        raise last_error
                logger.warning(
                    "workbench transient failure attempt=%d/%d kind=%s",
                    attempt + 1,
                    attempts,
                    type(last_error).__name__,
                )

        assert last_error is not None
        raise last_error

    @staticmethod
    def _classify(response: httpx.Response) -> tuple[LLMError, bool]:
        status = response.status_code
        if status == 429:
            return LLMRateLimitError(status_code=status), True
        if status in TRANSIENT_STATUSES:
            return LLMUnavailableError(status_code=status), True
        if status in (401, 403):
            blocked = "text/html" in response.headers.get("content-type", "")
            message = (
                "The request was blocked before reaching Workbench; check the approved network "
                "route or proxy."
                if blocked
                else None
            )
            return LLMAuthenticationError(message, status_code=status), False
        if status == 404:
            message = "Workbench endpoint, deployment or API version was not found."
            return LLMRequestRejectedError(message, status_code=status), False
        return LLMRequestRejectedError(status_code=status), False

    def _parse(self, response: httpx.Response, started: float) -> LLMResponse:
        invalid = LLMInvalidResponseError(status_code=response.status_code)
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise invalid from None
        if not isinstance(content, str) or not content.strip():
            raise invalid

        usage_raw = data.get("usage")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        model = data.get("model")
        finish = data["choices"][0].get("finish_reason")
        finish = finish if isinstance(finish, str) and _FINISH_REASON.match(finish) else None
        return LLMResponse(
            text=content,
            provider=PROVIDER_NAME,
            model=model if isinstance(model, str) and model else self._config.deployment,
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=TokenUsage(
                input_tokens=_count(usage_raw.get("prompt_tokens")),
                output_tokens=_count(usage_raw.get("completion_tokens")),
                total_tokens=_count(usage_raw.get("total_tokens")),
            ),
            http_status=response.status_code,
            finish_reason=finish,
        )
