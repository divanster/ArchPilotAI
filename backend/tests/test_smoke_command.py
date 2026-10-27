from typing import Any

import pytest

from app.core.config import Settings
from app.llm import smoke
from app.llm.contracts import LLMProvider, LLMRequest, LLMResponse
from app.llm.exceptions import LLMRateLimitError
from app.llm.fake_provider import FakeLLMProvider

CONFIRM = [smoke.CONFIRM_FLAG]


def never_called(_s: Settings) -> LLMProvider:
    raise AssertionError("provider must not be created")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(smoke.ENABLE_ENV, raising=False)
    monkeypatch.delenv("CI", raising=False)


def wb_settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, llm_provider="kpmg_workbench", require_postgres=False, **kw)


def test_disabled_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert smoke.run(CONFIRM, settings=wb_settings(), provider_factory=never_called) == 2
    assert "disabled" in capsys.readouterr().err


def test_requires_explicit_confirmation_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(smoke.ENABLE_ENV, "1")
    assert smoke.run([], settings=wb_settings(), provider_factory=never_called) == 2


def test_refuses_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(smoke.ENABLE_ENV, "1")
    monkeypatch.setenv("CI", "true")
    assert smoke.run(CONFIRM, settings=wb_settings(), provider_factory=never_called) == 2


def test_refuses_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(smoke.ENABLE_ENV, "1")
    settings = Settings(_env_file=None, require_postgres=False)
    assert smoke.run(CONFIRM, settings=settings, provider_factory=never_called) == 2


def test_single_attempt_and_no_content_in_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(smoke.ENABLE_ENV, "1")
    seen: list[Settings] = []
    fake = FakeLLMProvider({smoke.SYNTHETIC_PROMPT: "SECRET-ANSWER-CONTENT"})

    def factory(settings: Settings) -> LLMProvider:
        seen.append(settings)
        return fake

    code = smoke.run(
        CONFIRM, settings=wb_settings(workbench_max_retries=5), provider_factory=factory
    )
    out = capsys.readouterr().out
    assert code == 0
    assert seen[0].workbench_max_retries == 0
    assert len(fake.requests) == 1
    assert "SECRET-ANSWER-CONTENT" not in out
    assert smoke.SYNTHETIC_PROMPT not in out
    for label in ("http_status", "provider: fake", "model", "latency_ms", "total_tokens"):
        assert label in out


def test_failure_reports_status_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(smoke.ENABLE_ENV, "1")

    class Failing:
        provider_name = "kpmg_workbench"
        model_name = "m"

        def generate(self, request: LLMRequest) -> LLMResponse:
            raise LLMRateLimitError(status_code=429)

    assert smoke.run(CONFIRM, settings=wb_settings(), provider_factory=lambda _s: Failing()) == 1
    assert "http_status: 429" in capsys.readouterr().out
