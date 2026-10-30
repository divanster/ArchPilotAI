from urllib.parse import urlparse

from app.core.config import Settings
from app.llm.contracts import LLMProvider
from app.llm.exceptions import LLMConfigurationError
from app.llm.fake_provider import FakeLLMProvider
from app.llm.workbench_provider import (
    ReasoningEffort,
    WorkbenchConfig,
    WorkbenchProvider,
    is_reasoning_deployment,
)

_REQUIRED = {
    "ARCHPILOT_WORKBENCH_ENDPOINT": "workbench_endpoint",
    "ARCHPILOT_WORKBENCH_DEPLOYMENT": "workbench_deployment",
    "ARCHPILOT_WORKBENCH_API_VERSION": "workbench_api_version",
    "ARCHPILOT_WORKBENCH_REGION": "workbench_region",
    "ARCHPILOT_WORKBENCH_CHARGE_CODE": "workbench_charge_code",
}


def _require_url(value: str, setting: str, schemes: tuple[str, ...]) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        allowed = "/".join(schemes)
        raise LLMConfigurationError(
            f"{setting} must be a {allowed} URL without embedded credentials."
        )


def require_workbench_settings(settings: Settings, *, need_deployment: bool = True) -> None:
    """Shared by chat and embeddings; raises LLMConfigurationError naming only setting names."""
    required = {
        env: attr
        for env, attr in _REQUIRED.items()
        if need_deployment or attr != "workbench_deployment"
    }
    missing = [env for env, attr in required.items() if not str(getattr(settings, attr)).strip()]
    if not settings.workbench_api_key.get_secret_value().strip():
        missing.append("ARCHPILOT_WORKBENCH_API_KEY")
    if missing:
        raise LLMConfigurationError(
            "kpmg_workbench is selected but required settings are missing: "
            + ", ".join(sorted(missing))
        )
    _require_url(settings.workbench_endpoint, "ARCHPILOT_WORKBENCH_ENDPOINT", ("https",))
    if settings.workbench_proxy.strip():
        _require_url(settings.workbench_proxy, "ARCHPILOT_WORKBENCH_PROXY", ("http", "https"))


def deployment_settings(
    settings: Settings, deployment: str
) -> tuple[int | None, ReasoningEffort | None]:
    """(completion budget, reasoning effort) of a deployment: its own setting, else the global."""
    own = settings.workbench_deployments.get(deployment)
    effort = settings.workbench_reasoning_effort
    if own is not None and own.reasoning_effort is not None:
        effort = own.reasoning_effort
    budget: int | None = None
    if is_reasoning_deployment(deployment):
        budget = settings.workbench_max_completion_tokens_reasoning
        if own is not None and own.max_completion_tokens is not None:
            budget = own.max_completion_tokens
    return budget, effort or None


def verified_deployments(settings: Settings) -> list[str]:
    return sorted(name for name, d in settings.workbench_deployments.items() if d.verified)


def create_provider(settings: Settings) -> LLMProvider:
    """Build the configured provider. Never substitutes the fake for a misconfigured Workbench."""
    if settings.llm_provider == "fake":
        return FakeLLMProvider()

    require_workbench_settings(settings)
    deployment = settings.workbench_deployment.strip()
    budget, effort = deployment_settings(settings, deployment)

    return WorkbenchProvider(
        WorkbenchConfig(
            endpoint=settings.workbench_endpoint.strip(),
            deployment=deployment,
            api_version=settings.workbench_api_version.strip(),
            region=settings.workbench_region.strip(),
            charge_code=settings.workbench_charge_code.strip(),
            api_key=settings.workbench_api_key,
            proxy=settings.workbench_proxy.strip() or None,
            timeout=settings.workbench_timeout,
            max_retries=settings.workbench_max_retries,
            reasoning_effort=effort,
            max_completion_tokens=budget,
        )
    )
