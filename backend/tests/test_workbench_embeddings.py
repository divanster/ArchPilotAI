import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.embeddings.contracts import EmbeddingProviderError, EmbeddingValidationError
from app.embeddings.factory import create_embedding_provider
from app.embeddings.workbench import EmbeddingRouteError, WorkbenchEmbeddingProvider
from app.llm.exceptions import LLMConfigurationError
from app.llm.workbench_provider import WorkbenchConfig

API_KEY = "test-only-key-not-real"
CHARGE = "TEST-CHARGE-0000"
TEXT = "synthetic sentence one"


def config(**kw: Any) -> WorkbenchConfig:
    values: dict[str, Any] = {
        "endpoint": "https://workbench.example.test/",
        "deployment": "embed-test",
        "api_version": "2024-10-21",
        "region": "test-region",
        "charge_code": CHARGE,
        "api_key": SecretStr(API_KEY),
        "proxy": None,
        "timeout": 5,
        "max_retries": 0,
    }
    values.update(kw)
    return WorkbenchConfig(**values)


def ok_body(count: int, dim: int = 3, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "data": [{"index": i, "embedding": [1.0 + i] + [0.5] * (dim - 1)} for i in range(count)],
        "model": "text-embedding-test",
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }
    body.update(extra)
    return body


def provider(
    handler: Any, *, dimension: int | None = 3, batch: int = 16, **kw: Any
) -> WorkbenchEmbeddingProvider:
    return WorkbenchEmbeddingProvider(
        config(**kw),
        dimension=dimension,
        batch_size=batch,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def test_request_shape_and_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ok_body(2))

    result = provider(handler).embed(["a", "b"])
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/genai/azure/openai/deployments/embed-test/embeddings"
    assert request.url.params["api-version"] == "2024-10-21"
    assert request.headers["Ocp-Apim-Subscription-Key"] == API_KEY
    assert request.headers["x-kpmg-charge-code"] == CHARGE
    assert request.headers["x-kpmg-region-override"] == "test-region"
    assert json.loads(request.content) == {"input": ["a", "b"]}
    assert result.dimension == 3 and len(result.vectors) == 2
    assert result.model == "text-embedding-test"
    assert result.usage is not None and result.usage.input_tokens == 4


def test_batching_preserves_order_and_sums_usage() -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)["input"]
        calls.append(batch)
        data = [{"index": i, "embedding": [float(len(t)), 1.0, 1.0]} for i, t in enumerate(batch)]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 2}})

    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    result = provider(handler, batch=2).embed(texts)
    assert [len(c) for c in calls] == [2, 2, 1]
    assert [v[0] for v in result.vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert result.usage is not None and result.usage.input_tokens == 6
    assert result.usage.total_tokens is None


def test_out_of_order_indexes_are_reordered() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        body = ok_body(2)
        body["data"].reverse()
        return httpx.Response(200, json=body)

    result = provider(handler).embed(["a", "b"])
    assert result.vectors[0][0] == 1.0 and result.vectors[1][0] == 2.0


def test_unreported_model_and_usage_are_none() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ok_body(1)["data"]})

    result = provider(handler).embed([TEXT])
    assert result.model is None and result.usage is None


@pytest.mark.parametrize(
    "body",
    [
        {"data": []},
        {},
        {"data": "x"},
        {"data": [{"index": 0, "embedding": "nope"}]},
        {"data": [{"index": 0}]},
        {"data": [{"index": 5, "embedding": [1.0, 1.0, 1.0]}]},
        {"data": [{"index": True, "embedding": [1.0, 1.0, 1.0]}]},
        {"data": [{"index": 0, "embedding": [1.0, 1.0]}, {"index": 0, "embedding": [1.0, 1.0]}]},
        {"data": [{"index": 0, "embedding": [1.0, float("nan"), 1.0]}]},
        {"data": [{"index": 0, "embedding": [0.0, 0.0, 0.0]}]},
        {"data": [{"index": 0, "embedding": [1.0, "x", 1.0]}]},
        {"data": [{"index": 0, "embedding": [1.0, True, 1.0]}]},
        {"data": [{"index": 0, "embedding": [1.0, 1.0]}]},
        [1, 2],
    ],
)
def test_malformed_responses_rejected(body: Any) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body))

    with pytest.raises(EmbeddingValidationError):
        provider(handler).embed([TEXT])


def test_non_json_rejected() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(EmbeddingValidationError):
        provider(handler).embed([TEXT])


def test_inconsistent_dimensions_across_batches() -> None:
    sizes = iter([3, 4])

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body(1, dim=next(sizes)))

    with pytest.raises(EmbeddingValidationError):
        provider(handler, dimension=None, batch=1).embed(["a", "b"])


def test_dimension_mismatch_with_configured() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body(1, dim=4))

    with pytest.raises(EmbeddingValidationError):
        provider(handler, dimension=3).embed([TEXT])


def test_unconfigured_dimension_is_discovered() -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body(1, dim=5))

    p = provider(handler, dimension=None)
    assert p.embed([TEXT]).dimension == 5 and p.dimension == 0


@pytest.mark.parametrize("texts", [[], [""], ["  "], [1]])
def test_invalid_inputs_make_no_request(texts: Any) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(EmbeddingValidationError):
        provider(handler).embed(texts)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_errors_have_status_only(status: int) -> None:
    calls = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text=f"secret body {API_KEY}")

    p = provider(handler, max_retries=3)
    with pytest.raises(EmbeddingRouteError) as info:
        p.embed([TEXT])
    assert info.value.status_code == status and p.last_http_status == status
    assert calls == 1
    assert API_KEY not in str(info.value) and "secret body" not in str(info.value)


def test_zero_retries_on_transient() -> None:
    calls = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(EmbeddingRouteError):
        provider(handler, max_retries=0).embed([TEXT])
    assert calls == 1


def test_retries_then_success() -> None:
    codes = iter([429, 500])

    def handler(_r: httpx.Request) -> httpx.Response:
        code = next(codes, 200)
        return httpx.Response(code, json=ok_body(1) if code == 200 else {})

    assert len(provider(handler, max_retries=2).embed([TEXT]).vectors) == 1


def test_timeout_and_transport_errors() -> None:
    def timeout(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"boom {API_KEY}")

    def refused(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {API_KEY}")

    for handler in (timeout, refused):
        with pytest.raises(EmbeddingProviderError) as info:
            provider(handler).embed([TEXT])
        assert API_KEY not in str(info.value)


def test_secret_not_in_repr_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=TEXT)

    p = provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingRouteError):
        p.embed([TEXT])
    assert API_KEY not in repr(p) and API_KEY not in caplog.text
    assert TEXT not in caplog.text and CHARGE not in caplog.text


def test_identity_flags() -> None:
    p = provider(lambda _r: httpx.Response(200, json=ok_body(1)))
    assert p.provider_name == "kpmg_workbench"
    assert p.model_name == "embed-test"
    assert p.is_test_provider is False


@pytest.mark.parametrize("kwargs", [{"batch": 0}, {"batch": 5000}, {"dimension": 0}])
def test_constructor_validation(kwargs: Any) -> None:
    with pytest.raises(ValueError):
        provider(lambda _r: httpx.Response(200), **kwargs)


def _settings(**kw: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "embedding_provider": "kpmg_workbench",
        "embedding_deployment": "embed-test",
        "embedding_dimension": 3,
        "workbench_api_key": SecretStr(API_KEY),
        "workbench_endpoint": "https://workbench.example.test",
        "workbench_api_version": "2024-10-21",
        "workbench_region": "test-region",
        "workbench_charge_code": CHARGE,
    }
    values.update(kw)
    return Settings(_env_file=None, **values)


def test_factory_default_is_none() -> None:
    assert create_embedding_provider(Settings(_env_file=None, database_url="sqlite://")) is None


def test_factory_builds_workbench_provider() -> None:
    p = create_embedding_provider(_settings())
    assert isinstance(p, WorkbenchEmbeddingProvider)
    assert p.model_name == "embed-test" and p.dimension == 3


@pytest.mark.parametrize(
    "override",
    [
        {"embedding_deployment": ""},
        {"embedding_dimension": 0},
        {"workbench_endpoint": ""},
        {"workbench_endpoint": "http://insecure.example.test"},
        {"workbench_api_key": SecretStr("")},
    ],
)
def test_factory_rejects_incomplete_configuration(override: Any) -> None:
    with pytest.raises(LLMConfigurationError) as info:
        create_embedding_provider(_settings(**override))
    assert API_KEY not in str(info.value)
