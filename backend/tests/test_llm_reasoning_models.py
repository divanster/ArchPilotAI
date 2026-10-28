"""Model-specific Workbench request compatibility. Mocked transport only; no network."""

import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.llm.contracts import LLMRequest
from app.llm.exceptions import LLMInvalidResponseError
from app.llm.factory import create_provider
from app.llm.workbench_provider import WorkbenchConfig, WorkbenchProvider, is_reasoning_deployment

CANDIDATE = "gpt-5-6-sol-2026-07-09-dzs-sdc"
BASELINE = "gpt-4-1-2025-04-14-dzs-sdc"
API_KEY = "test-only-key-not-real"
REQ = LLMRequest(prompt="p", system_prompt="s", temperature=0.0, max_output_tokens=8000)
OK = {
    "model": "m",
    "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
}


def send(deployment: str, body: dict[str, Any] = OK, **kw: Any) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    config = WorkbenchConfig(
        endpoint="https://workbench.example.test",
        deployment=deployment,
        api_version="2024-02-01",
        region="r",
        charge_code="c",
        api_key=API_KEY,
        max_retries=0,
        **kw,
    )
    WorkbenchProvider(config, transport=httpx.MockTransport(handler)).generate(REQ)
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (CANDIDATE, True),
        ("gpt-5-mini-2025", True),
        ("o3-mini", True),
        (BASELINE, False),
        ("gpt-4o", False),
        ("gpt-50", False),
        ("", False),
    ],
)
def test_reasoning_deployment_detection(name: str, expected: bool) -> None:
    assert is_reasoning_deployment(name) is expected


def test_gpt41_body_is_unchanged() -> None:
    body = send(BASELINE)
    assert body == {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "p"},
        ],
        "temperature": 0.0,
        "max_tokens": 8000,
        "stream": False,
    }


def test_gpt41_ignores_reasoning_effort() -> None:
    assert "reasoning_effort" not in send(BASELINE, reasoning_effort="high")


def test_candidate_uses_max_completion_tokens_without_unsupported_params() -> None:
    body = send(CANDIDATE)
    assert body["max_completion_tokens"] == 8000
    for unsupported in ("max_tokens", "temperature", "top_p", "logprobs"):
        assert unsupported not in body
    assert "reasoning_effort" not in body  # model default unless explicitly configured
    assert body["messages"][0] == {"role": "system", "content": "s"}
    assert body["stream"] is False


def test_candidate_reasoning_effort_only_when_configured() -> None:
    assert send(CANDIDATE, reasoning_effort="low")["reasoning_effort"] == "low"


def test_reasoning_exhausted_output_fails_as_invalid_response_without_leaking() -> None:
    truncated = {
        "model": "m",
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 8000, "total_tokens": 8001},
    }
    with pytest.raises(LLMInvalidResponseError) as info:
        send(CANDIDATE, truncated)
    assert API_KEY not in str(info.value)


def test_effort_setting_flows_through_factory() -> None:
    values: dict[str, Any] = {
        "require_postgres": False,
        "llm_provider": "kpmg_workbench",
        "workbench_api_key": API_KEY,
        "workbench_endpoint": "https://workbench.example.test",
        "workbench_deployment": CANDIDATE,
        "workbench_api_version": "2024-02-01",
        "workbench_region": "r",
        "workbench_charge_code": "c",
        "workbench_reasoning_effort": "medium",
    }
    provider = create_provider(Settings(_env_file=None, **values))
    assert isinstance(provider, WorkbenchProvider)
    assert provider._config.reasoning_effort == "medium"
    values["workbench_reasoning_effort"] = ""
    default = create_provider(Settings(_env_file=None, **values))
    assert isinstance(default, WorkbenchProvider)
    assert default._config.reasoning_effort is None
