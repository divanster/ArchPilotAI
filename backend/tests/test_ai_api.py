import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import get_settings
from app.llm.contracts import LLMResponse, TokenUsage
from app.llm.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.llm.fake_provider import FakeLLMProvider
from app.main import create_app
from app.services.ai_service import AIService

URL = "/api/ai/generate"
SECRET_BODY = "UPSTREAM-SECRET-BODY"


def make_service(provider: object, interval: float = 0.0, max_chars: int = 4000) -> AIService:
    return AIService(
        provider,  # type: ignore[arg-type]
        max_prompt_chars=max_chars,
        max_output_tokens=400,
        min_interval_seconds=interval,
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[deps.get_ai_service] = lambda: make_service(FakeLLMProvider())
    yield TestClient(app)


def use(client: TestClient, service: AIService) -> None:
    client.app.dependency_overrides[deps.get_ai_service] = lambda: service  # type: ignore[attr-defined]


def test_generate_returns_documented_shape(client: TestClient) -> None:
    response = client.post(URL, json={"prompt": "Explain the benefits of a modular monolith."})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"answer", "provider", "model", "latency_ms", "usage"}
    assert body["answer"].startswith("Fake answer to: Explain")
    assert (body["provider"], body["model"]) == ("fake", "fake-model-1")
    assert set(body["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}


def test_usage_none_is_passed_through_not_fabricated(client: TestClient) -> None:
    class NoUsage:
        provider_name = "kpmg_workbench"
        model_name = "m"

        def generate(self, request: object) -> LLMResponse:
            return LLMResponse(
                text="a", provider="kpmg_workbench", model="m", latency_ms=1, usage=TokenUsage()
            )

    use(client, make_service(NoUsage()))
    usage = client.post(URL, json={"prompt": "hi"}).json()["usage"]
    assert usage == {"input_tokens": None, "output_tokens": None, "total_tokens": None}


@pytest.mark.parametrize("payload", [{}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 5}])
def test_invalid_prompts_are_422(client: TestClient, payload: dict[str, object]) -> None:
    assert client.post(URL, json=payload).status_code == 422


def test_configured_prompt_limit(client: TestClient) -> None:
    use(client, make_service(FakeLLMProvider(), max_chars=10))
    assert client.post(URL, json={"prompt": "x" * 11}).status_code == 422
    assert client.post(URL, json={"prompt": "x" * 10}).status_code == 200


def test_hard_schema_limit(client: TestClient) -> None:
    assert client.post(URL, json={"prompt": "x" * 20_001}).status_code == 422


def test_output_is_truncated(client: TestClient) -> None:
    use(client, make_service(FakeLLMProvider({"q": "y" * 20_000})))
    assert len(client.post(URL, json={"prompt": "q"}).json()["answer"]) == 16_000


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMAuthenticationError(status_code=401), 502),
        (LLMRateLimitError(status_code=429), 429),
        (LLMTimeoutError(), 504),
        (LLMInvalidResponseError(), 502),
    ],
)
def test_provider_errors_are_mapped_safely(
    client: TestClient, error: LLMError, expected: int
) -> None:
    use(client, make_service(FakeLLMProvider(error=error)))
    response = client.post(URL, json={"prompt": "hi"})
    assert response.status_code == expected
    assert response.json() == {"detail": error.public_message}
    assert SECRET_BODY not in response.text


def test_requests_are_throttled(client: TestClient) -> None:
    use(client, make_service(FakeLLMProvider(), interval=60))
    assert client.post(URL, json={"prompt": "a"}).status_code == 200
    second = client.post(URL, json={"prompt": "b"})
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1


def test_failed_requests_also_count_for_throttling(client: TestClient) -> None:
    use(client, make_service(FakeLLMProvider(error=LLMTimeoutError()), interval=60))
    assert client.post(URL, json={"prompt": "a"}).status_code == 504
    assert client.post(URL, json={"prompt": "b"}).status_code == 429


def test_concurrent_requests_are_rejected() -> None:
    import threading

    started, release = threading.Event(), threading.Event()

    class Slow(FakeLLMProvider):
        def generate(self, request):  # type: ignore[no-untyped-def]
            started.set()
            release.wait(5)
            return super().generate(request)

    service = make_service(Slow())
    results: list[str] = []
    worker = threading.Thread(target=lambda: results.append(service.generate("a").text))
    worker.start()
    assert started.wait(5)
    from app.services.ai_service import AIBusyError

    with pytest.raises(AIBusyError):
        service.generate("b")
    release.set()
    worker.join(5)
    assert results == ["Fake answer to: a"]
    assert service.generate("c").text  # slot is released afterwards


def test_disabled_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHPILOT_ENVIRONMENT", "production")
    monkeypatch.setenv("ARCHPILOT_REQUIRE_POSTGRES", "0")
    get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[deps.get_ai_service] = lambda: make_service(FakeLLMProvider())
        response = TestClient(app).post(URL, json={"prompt": "hi"})
        assert response.status_code == 403
    finally:
        get_settings.cache_clear()


def test_missing_workbench_config_is_503_without_fake_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHPILOT_LLM_PROVIDER", "kpmg_workbench")
    get_settings.cache_clear()
    deps._build_ai_service.cache_clear()
    try:
        response = TestClient(create_app()).post(URL, json={"prompt": "hi"})
        assert response.status_code == 503
        assert "ARCHPILOT_WORKBENCH_API_KEY" in response.json()["detail"]
        assert "Fake" not in response.text
    finally:
        get_settings.cache_clear()
        deps._build_ai_service.cache_clear()


def test_prompts_and_answers_are_not_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        client.post(URL, json={"prompt": "very-private-synthetic-prompt"})
    assert "very-private-synthetic-prompt" not in caplog.text
    assert "ai generation provider=fake" in caplog.text
