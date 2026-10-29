import logging
import re
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from app.domain.embedding_math import validate_vector
from app.embeddings.contracts import (
    EmbeddingProviderError,
    EmbeddingResult,
    EmbeddingUsage,
    EmbeddingValidationError,
)
from app.llm.workbench_provider import (
    BACKOFF_BASE_SECONDS,
    TRANSIENT_STATUSES,
    WorkbenchConfig,
    _count,
    _retry_after,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "kpmg_workbench"
EMBEDDER_VERSION = "workbench-v1"
# Verified by a real probe: deployment text-embedding-3-large-1-std-sdc (model
# text-embedding-3-large, dimension 3072) answers on the chat route's pattern with
# "embeddings" in place of "chat/completions". The dimension is read from settings
# (ARCHPILOT_EMBEDDING_DIMENSION) and every response is validated against it.
EMBEDDINGS_PATH = "/genai/azure/openai/deployments/{deployment}/embeddings"
MAX_BATCH_SIZE = 2048

_MODEL = re.compile(r"^[\w.\-:/]{1,100}$")


class EmbeddingRouteError(EmbeddingProviderError):
    """Carries only the upstream HTTP status (safe to report)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorkbenchEmbeddingProvider:
    """Workbench embeddings over httpx, reusing the chat provider's headers, proxy and timeout.

    The route follows the chat route's pattern (see ``EMBEDDINGS_PATH``). TLS verification is on.
    Inputs, vectors, headers and upstream bodies are never logged or surfaced.
    """

    provider_name = PROVIDER_NAME
    embedder_version = EMBEDDER_VERSION
    is_test_provider = False

    def __init__(
        self,
        config: WorkbenchConfig,
        *,
        dimension: int | None,
        batch_size: int = 16,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError("batch_size out of range")
        if dimension is not None and dimension < 1:
            raise ValueError("dimension must be positive")
        self._config = config
        self._expected = dimension
        self._batch_size = batch_size
        self._transport = transport
        self._sleep = sleep
        self.last_http_status: int | None = None
        self.last_latency_ms: int | None = None

    @property
    def model_name(self) -> str:
        return self._config.deployment

    @property
    def dimension(self) -> int:
        # 0 means "not configured"; only the unconfigured probe path uses it.
        return self._expected or 0

    def __repr__(self) -> str:
        return f"WorkbenchEmbeddingProvider(deployment={self._config.deployment!r})"

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
        path = EMBEDDINGS_PATH.format(deployment=quote(self._config.deployment, safe=""))
        return self._config.endpoint.rstrip("/") + path

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        if not texts or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise EmbeddingValidationError("inputs must be non-empty strings")
        started = time.perf_counter()
        vectors: list[list[float]] = []
        model: str | None = None
        input_tokens: int | None = None
        total_tokens: int | None = None
        with self._client() as client:
            for start in range(0, len(texts), self._batch_size):
                batch = list(texts[start : start + self._batch_size])
                data = self._post(client, batch)
                vectors.extend(self._vectors(data, len(batch)))
                reported = data.get("model")
                if model is None and isinstance(reported, str) and _MODEL.match(reported):
                    model = reported
                raw_usage = data.get("usage")
                usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
                for key, current in (
                    ("prompt_tokens", input_tokens),
                    ("total_tokens", total_tokens),
                ):
                    value = _count(usage.get(key))
                    if value is not None:
                        total = (current or 0) + value
                        if key == "prompt_tokens":
                            input_tokens = total
                        else:
                            total_tokens = total
        dimension = len(vectors[0])
        if any(not isinstance(v, list) or len(v) != dimension for v in vectors):
            raise EmbeddingValidationError("vectors have inconsistent dimensions")
        vectors = [validate_vector(v, dimension) for v in vectors]
        if self._expected is not None and dimension != self._expected:
            raise EmbeddingValidationError("vector dimension differs from the configured one")
        self.last_latency_ms = int((time.perf_counter() - started) * 1000)
        usage_out = (
            EmbeddingUsage(input_tokens=input_tokens, total_tokens=total_tokens)
            if input_tokens is not None or total_tokens is not None
            else None
        )
        return EmbeddingResult(vectors=vectors, dimension=dimension, model=model, usage=usage_out)

    def _post(self, client: httpx.Client, batch: list[str]) -> dict[str, Any]:
        attempts = self._config.max_retries + 1
        wait: float | None = None
        error: EmbeddingProviderError | None = None
        for attempt in range(attempts):
            if attempt > 0:
                self._sleep(wait if wait is not None else BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            wait = None
            try:
                response = client.post(
                    self._url(),
                    params={"api-version": self._config.api_version},
                    headers=self._headers(),
                    json={"input": batch},
                )
            except httpx.TimeoutException:
                error = EmbeddingProviderError("The embedding request timed out")
            except httpx.TransportError as exc:
                logger.warning("workbench embedding transport error type=%s", type(exc).__name__)
                error = EmbeddingProviderError("The embedding service is unreachable")
            else:
                self.last_http_status = response.status_code
                if response.is_success:
                    try:
                        data = response.json()
                    except ValueError:
                        raise EmbeddingValidationError("unexpected response") from None
                    if not isinstance(data, dict):
                        raise EmbeddingValidationError("unexpected response")
                    return data
                retryable = (
                    response.status_code == 429 or response.status_code in TRANSIENT_STATUSES
                )
                error = EmbeddingRouteError(
                    "The embedding request was rejected", response.status_code
                )
                if not retryable:
                    logger.warning("workbench embedding failed status=%s", response.status_code)
                    raise error
                wait = _retry_after(response)
        assert error is not None
        raise error

    @staticmethod
    def _vectors(data: dict[str, Any], expected: int) -> list[list[float]]:
        items = data.get("data")
        if not isinstance(items, list) or len(items) != expected:
            raise EmbeddingValidationError("wrong number of vectors in the response")
        ordered: list[list[float] | None] = [None] * expected
        for item in items:
            index = item.get("index") if isinstance(item, dict) else None
            vector = item.get("embedding") if isinstance(item, dict) else None
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < expected
                or ordered[index] is not None
                or not isinstance(vector, list)
            ):
                raise EmbeddingValidationError("malformed embedding item")
            ordered[index] = vector
        # Numeric/finite/nonzero checks happen in validate_vector; here only shape.
        return [v for v in ordered if v is not None]
