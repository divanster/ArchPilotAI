"""Opt-in, single-request Workbench smoke test. NOT run by CI or the test suite.

    $env:ARCHPILOT_LLM_PROVIDER = "kpmg_workbench"   # plus the other ARCHPILOT_WORKBENCH_* settings
    $env:ARCHPILOT_WORKBENCH_SMOKE_TEST = "1"
    python -m app.llm.smoke --confirm-authorized-for-archpilotai

Makes at most one real inference request with a short synthetic prompt, never retries, writes
nothing to any database and prints only status, provider, model, latency and token counts.
"""

import argparse
import os
import sys
from collections.abc import Callable, Sequence

from app.core.config import Settings, get_settings
from app.llm.contracts import LLMProvider, LLMRequest
from app.llm.exceptions import LLMConfigurationError, LLMError
from app.llm.factory import create_provider

ENABLE_ENV = "ARCHPILOT_WORKBENCH_SMOKE_TEST"
CONFIRM_FLAG = "--confirm-authorized-for-archpilotai"
SYNTHETIC_PROMPT = "Reply with the single word: ready."


def _fmt(value: object) -> str:
    return "not reported" if value is None else str(value)


def run(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    provider_factory: Callable[[Settings], LLMProvider] = create_provider,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.llm.smoke", description=__doc__)
    parser.add_argument(
        CONFIRM_FLAG,
        action="store_true",
        dest="confirmed",
        help="confirm the credentials and charge code are authorized for ArchPilotAI",
    )
    args = parser.parse_args(argv)

    if os.environ.get("CI", "").lower() in {"1", "true"}:
        print("Refusing to run in CI.", file=sys.stderr)
        return 2
    if os.environ.get(ENABLE_ENV) != "1" or not args.confirmed:
        print(
            f"Smoke test disabled. Set {ENABLE_ENV}=1 and pass {CONFIRM_FLAG} after confirming "
            "the credentials and charge code are authorized for ArchPilotAI.",
            file=sys.stderr,
        )
        return 2

    settings = settings or get_settings()
    if settings.llm_provider != "kpmg_workbench":
        print("ARCHPILOT_LLM_PROVIDER must be kpmg_workbench.", file=sys.stderr)
        return 2
    # Exactly one attempt: retries are disabled for this command.
    settings = settings.model_copy(update={"workbench_max_retries": 0})
    try:
        provider = provider_factory(settings)
    except LLMConfigurationError as exc:
        print(f"Configuration error: {exc.public_message}", file=sys.stderr)
        return 2

    try:
        result = provider.generate(LLMRequest(prompt=SYNTHETIC_PROMPT, max_output_tokens=16))
    except LLMError as exc:
        print(f"FAILED: {exc.public_message}")
        print(f"http_status: {_fmt(exc.status_code)}")
        print(f"provider: {provider.provider_name}")
        print(f"model: {provider.model_name}")
        return 1

    print("OK")
    print(f"http_status: {_fmt(result.http_status)}")
    print(f"provider: {result.provider}")
    print(f"model: {result.model}")
    print(f"latency_ms: {result.latency_ms}")
    print(f"input_tokens: {_fmt(result.usage.input_tokens)}")
    print(f"output_tokens: {_fmt(result.usage.output_tokens)}")
    print(f"total_tokens: {_fmt(result.usage.total_tokens)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
