import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.llm.contracts import LLMProvider, LLMRequest
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMRequestRejectedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.factory import create_provider
from app.llm.fake_provider import FakeLLMProvider
from app.llm.workbench_provider import WorkbenchConfig, WorkbenchProvider

API_KEY = "test-only-key-not-real"
CHARGE = "TEST-CHARGE-0000"
PROMPT = "synthetic prompt about a modular monolith"
ANSWER = "synthetic model answer text"
REQ = LLMRequest(prompt=PROMPT, system_prompt="synthetic system", max_output_tokens=50)


def config(**kw: object) -> WorkbenchConfig:
    values: dict[str, Any] = {
        "endpoint": "https://workbench.example.test/",
        "deployment": "gpt-test",
        "api_version": "2024-02-01",
        "region": "test-region",
        "charge_code": CHARGE,
        "api_key": API_KEY,
        "max_retries": 2,
        "timeout": 5.0,
    }
    values.update(kw)
    return WorkbenchConfig(**values)


OK_BODY = {
    "model": "gpt-test-0613",
    "choices": [{"message": {"role": "assistant", "content": ANSWER}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


def provider_for(
    handler: Callable[[httpx.Request], httpx.Response], **kw: object
) -> tuple[WorkbenchProvider, list[float]]:
    sleeps: list[float] = []
    provider = WorkbenchProvider(
        config(**kw), transport=httpx.MockTransport(handler), sleep=sleeps.append
    )
    return provider, sleeps


def settings(**kw: Any) -> Settings:
    values: dict[str, Any] = {"require_postgres": False}
    values.update(kw)
    return Settings(_env_file=None, **values)


WORKBENCH_SETTINGS = {
    "llm_provider": "kpmg_workbench",
    "workbench_api_key": API_KEY,
    "workbench_endpoint": "https://workbench.example.test",
    "workbench_deployment": "gpt-test",
    "workbench_api_version": "2024-02-01",
    "workbench_region": "test-region",
    "workbench_charge_code": CHARGE,
}


# --- fake provider ---------------------------------------------------------------------------


def test_fake_provider_is_deterministic_and_offline() -> None:
    provider = FakeLLMProvider()
    assert isinstance(provider, LLMProvider)
    first = provider.generate(LLMRequest(prompt="hello world"))
    second = provider.generate(LLMRequest(prompt="hello world"))
    assert first.text == second.text == "Fake answer to: hello world"
    assert (first.provider, first.model) == ("fake", "fake-model-1")
    assert first.usage.input_tokens == 2


def test_fake_provider_configurable_response_and_error() -> None:
    provider = FakeLLMProvider({"q": "canned"})
    assert provider.generate(LLMRequest(prompt="q")).text == "canned"
    failing = FakeLLMProvider(error=LLMTimeoutError())
    with pytest.raises(LLMTimeoutError):
        failing.generate(LLMRequest(prompt="q"))


# --- factory / configuration -----------------------------------------------------------------


def test_default_provider_is_fake() -> None:
    assert Settings(_env_file=None).llm_provider == "fake"
    assert isinstance(create_provider(settings()), FakeLLMProvider)


def test_factory_builds_workbench_provider() -> None:
    provider = create_provider(settings(**WORKBENCH_SETTINGS))
    assert isinstance(provider, WorkbenchProvider)
    assert (provider.provider_name, provider.model_name) == ("kpmg_workbench", "gpt-test")
    assert API_KEY not in repr(provider)


def test_missing_workbench_configuration_fails_clearly_without_fallback() -> None:
    with pytest.raises(LLMConfigurationError) as info:
        create_provider(settings(llm_provider="kpmg_workbench"))
    message = str(info.value)
    for name in ("API_KEY", "ENDPOINT", "DEPLOYMENT", "API_VERSION", "REGION", "CHARGE_CODE"):
        assert f"ARCHPILOT_WORKBENCH_{name}" in message


def test_missing_api_key_only() -> None:
    values = {**WORKBENCH_SETTINGS, "workbench_api_key": "  "}
    with pytest.raises(LLMConfigurationError, match="ARCHPILOT_WORKBENCH_API_KEY"):
        create_provider(settings(**values))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workbench_endpoint", "http://workbench.example.test"),
        ("workbench_endpoint", "https://user:pw@workbench.example.test"),
        ("workbench_proxy", "socks5://proxy.example.test:1080"),
    ],
)
def test_invalid_urls_are_rejected(field: str, value: str) -> None:
    with pytest.raises(LLMConfigurationError):
        create_provider(settings(**{**WORKBENCH_SETTINGS, field: value}))


def test_proxy_is_optional_and_validated() -> None:
    provider = create_provider(
        settings(**{**WORKBENCH_SETTINGS, "workbench_proxy": "http://proxy.example.test:8080"})
    )
    assert isinstance(provider, WorkbenchProvider)


@pytest.mark.parametrize(
    "bad",
    [
        {"llm_provider": "openai"},
        {"workbench_auth_type": "oauth"},
        {"workbench_timeout": 0},
        {"workbench_max_retries": 99},
    ],
)
def test_settings_validation(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        settings(**bad)


def test_api_key_is_secret_in_settings_repr() -> None:
    assert API_KEY not in repr(settings(**WORKBENCH_SETTINGS))


# --- workbench provider: request contract ----------------------------------------------------


def test_successful_response_matches_documented_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    provider, sleeps = provider_for(handler)
    result = provider.generate(REQ)

    request = seen[0]
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.path == "/genai/azure/openai/deployments/gpt-test/chat/completions"
    assert request.url.params["api-version"] == "2024-02-01"
    assert API_KEY not in str(request.url)
    assert request.headers["Ocp-Apim-Subscription-Key"] == API_KEY
    assert request.headers["x-kpmg-charge-code"] == CHARGE
    assert request.headers["x-kpmg-region-override"] == "test-region"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body["messages"] == [
        {"role": "system", "content": "synthetic system"},
        {"role": "user", "content": PROMPT},
    ]
    assert (body["max_tokens"], body["stream"]) == (50, False)
    assert API_KEY not in request.content.decode()

    assert result.text == ANSWER
    assert (result.provider, result.model, result.http_status) == (
        "kpmg_workbench",
        "gpt-test-0613",
        200,
    )
    assert result.usage.model_dump() == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
    assert result.latency_ms >= 0
    assert sleeps == []


def test_deployment_is_url_quoted() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json=OK_BODY)

    provider, _ = provider_for(handler, deployment="a/b c")
    provider.generate(REQ)
    assert "/deployments/a%2Fb%20c/chat/completions" in seen[0]


def test_missing_token_usage_is_none_not_fabricated() -> None:
    body = {"choices": [{"message": {"content": ANSWER}}]}
    provider, _ = provider_for(lambda _r: httpx.Response(200, json=body))
    result = provider.generate(REQ)
    assert result.usage.model_dump() == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    assert result.model == "gpt-test"  # falls back to the configured deployment


def test_partial_and_malformed_usage() -> None:
    body = {
        "choices": [{"message": {"content": ANSWER}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": "7", "total_tokens": True},
    }
    provider, _ = provider_for(lambda _r: httpx.Response(200, json=body))
    usage = provider.generate(REQ).usage
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (5, None, None)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
        httpx.Response(200, json=["unexpected"]),
    ],
)
def test_invalid_response_is_rejected(response: httpx.Response) -> None:
    provider, sleeps = provider_for(lambda _r: response)
    with pytest.raises(LLMInvalidResponseError):
        provider.generate(REQ)
    assert sleeps == []


# --- workbench provider: failures and retries ------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure_is_not_retried(status: int) -> None:
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"message": f"secret detail {API_KEY}"})

    provider, sleeps = provider_for(handler)
    with pytest.raises(LLMAuthenticationError) as info:
        provider.generate(REQ)
    assert info.value.status_code == status
    assert "secret detail" not in info.value.public_message
    assert len(calls) == 1
    assert sleeps == []


def test_html_block_page_gives_proxy_hint_without_body() -> None:
    html = httpx.Response(
        403, headers={"content-type": "text/html"}, text="<html>blocked by WAF xyz</html>"
    )
    provider, _ = provider_for(lambda _r: html)
    with pytest.raises(LLMAuthenticationError) as info:
        provider.generate(REQ)
    assert "proxy" in info.value.public_message
    assert "WAF" not in info.value.public_message


def test_not_found_and_other_client_errors_are_not_retried() -> None:
    for status, expected in ((404, "not found"), (400, "rejected")):
        provider, sleeps = provider_for(_respond(status))
        with pytest.raises(LLMRequestRejectedError, match=expected):
            provider.generate(REQ)
        assert sleeps == []


def test_missing_credentials_never_reach_the_network() -> None:
    # Misconfiguration fails in the factory, before any provider or HTTP client exists.
    with pytest.raises(LLMConfigurationError):
        create_provider(
            settings(llm_provider="kpmg_workbench", workbench_endpoint="https://x.test")
        )


def test_timeout_is_retried_then_reported() -> None:
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("timed out")

    provider, sleeps = provider_for(handler, max_retries=2)
    with pytest.raises(LLMTimeoutError):
        provider.generate(REQ)
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff


def test_rate_limit_honours_retry_after_then_exhausts() -> None:
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "3"}, json={"error": "slow down"})

    provider, sleeps = provider_for(handler, max_retries=2)
    with pytest.raises(LLMRateLimitError) as info:
        provider.generate(REQ)
    assert info.value.status_code == 429
    assert len(calls) == 3
    assert sleeps == [3.0, 3.0]


def test_retry_after_is_capped() -> None:
    provider, sleeps = provider_for(
        lambda _r: httpx.Response(429, headers={"retry-after": "9999"}), max_retries=1
    )
    with pytest.raises(LLMRateLimitError):
        provider.generate(REQ)
    assert sleeps == [30.0]


def test_transient_error_then_success() -> None:
    responses: list[httpx.Response | Exception] = [
        httpx.Response(503),
        httpx.ConnectError("down"),
        httpx.Response(200, json=OK_BODY),
    ]
    queue = iter(responses)

    def handler(_r: httpx.Request) -> httpx.Response:
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        return item

    provider, sleeps = provider_for(handler, max_retries=2)
    assert provider.generate(REQ).text == ANSWER
    assert sleeps == [0.5, 1.0]


def test_server_errors_exhaust_retries() -> None:
    provider, sleeps = provider_for(lambda _r: httpx.Response(502, text="bad gateway body"))
    with pytest.raises(LLMUnavailableError) as info:
        provider.generate(REQ)
    assert info.value.status_code == 502
    assert len(sleeps) == 2


def test_zero_retries_makes_a_single_attempt() -> None:
    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    provider, sleeps = provider_for(handler, max_retries=0)
    with pytest.raises(LLMUnavailableError):
        provider.generate(REQ)
    assert (len(calls), sleeps) == (1, [])


def test_tls_verification_is_never_disabled() -> None:
    import inspect

    from app.llm import workbench_provider

    assert "verify" not in inspect.getsource(workbench_provider).replace("TLS verification", "")


# --- secret redaction ------------------------------------------------------------------------


def test_logs_never_contain_secrets_prompts_or_answers(caplog: pytest.LogCaptureFixture) -> None:
    responses = iter(
        [httpx.Response(503, text=f"upstream {PROMPT}"), httpx.Response(200, json=OK_BODY)]
    )
    provider, _ = provider_for(lambda _r: next(responses))
    with caplog.at_level(logging.DEBUG):
        provider.generate(REQ)
        failing, _ = provider_for(lambda _r: httpx.Response(401, text=API_KEY))
        with pytest.raises(LLMAuthenticationError):
            failing.generate(REQ)
    for secret in (API_KEY, CHARGE, PROMPT, ANSWER, "Ocp-Apim"):
        assert secret not in caplog.text


def test_errors_do_not_leak_secrets() -> None:
    provider, _ = provider_for(
        lambda _r: httpx.Response(500, text=f"{API_KEY} {PROMPT}"), max_retries=0
    )
    with pytest.raises(LLMUnavailableError) as info:
        provider.generate(REQ)
    assert API_KEY not in str(info.value) + repr(info.value)
    assert PROMPT not in str(info.value)


def _respond(status: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(status, text="internal")
