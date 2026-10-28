from urllib.parse import urlparse

from app.core.config import Settings
from app.llm.contracts import LLMProvider
from app.llm.exceptions import LLMConfigurationError
from app.llm.fake_provider import FakeLLMProvider
from app.llm.workbench_provider import WorkbenchConfig, WorkbenchProvider

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


def create_provider(settings: Settings) -> LLMProvider:
    """Build the configured provider. Never substitutes the fake for a misconfigured Workbench."""
    if settings.llm_provider == "fake":
        return FakeLLMProvider()

    missing = [env for env, attr in _REQUIRED.items() if not str(getattr(settings, attr)).strip()]
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

    return WorkbenchProvider(
        WorkbenchConfig(
            endpoint=settings.workbench_endpoint.strip(),
            deployment=settings.workbench_deployment.strip(),
            api_version=settings.workbench_api_version.strip(),
            region=settings.workbench_region.strip(),
            charge_code=settings.workbench_charge_code.strip(),
            api_key=settings.workbench_api_key,
            proxy=settings.workbench_proxy.strip() or None,
            timeout=settings.workbench_timeout,
            max_retries=settings.workbench_max_retries,
            reasoning_effort=settings.workbench_reasoning_effort or None,
        )
    )
