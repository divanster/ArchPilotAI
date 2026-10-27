# ADR 0003: LLMProvider abstraction and KPMG Workbench provider

Status: Accepted (local prototype)

## Context

Phase 4 adds the first AI call. Workbench must be used only through the `LLMProvider`
abstraction so it stays replaceable and testable offline.

## Decision

- `app/llm/contracts.py` defines Pydantic `LLMRequest`/`LLMResponse` and the `LLMProvider`
  Protocol (provider, model, latency, optional token usage).
- `FakeLLMProvider` is the default: deterministic, no network, no credentials.
- `WorkbenchProvider` (httpx) calls
  `POST {endpoint}/genai/azure/openai/deployments/{deployment}/chat/completions?api-version=...`
  with headers `Ocp-Apim-Subscription-Key`, `x-kpmg-charge-code`, `x-kpmg-region-override`,
  `Accept` and `Content-Type`. It uses an explicitly configured proxy, never disables TLS
  verification, applies a timeout, and retries timeouts, transport errors, 429 and 5xx
  (502/503/504, 500) with bounded exponential backoff. 401/403 are not retried.
- The factory selects the provider from `ARCHPILOT_LLM_PROVIDER`; a misconfigured Workbench
  fails with a configuration error and never falls back to the fake.
- `AIService` enforces prompt/output limits, one in-flight request and a minimum interval
  between requests to bound cost. The endpoint returns 403 in production.
- Token usage is reported only when Workbench returns it; otherwise it is null.

## Consequences / limitations

- Guards are per process; there is no authentication, so this is not safe for shared use.
- Deployment discovery (`GET /genai/azure/deployments/openai`) is not implemented.
- `ARCHPILOT_WORKBENCH_APPROVAL_GROUP` is accepted but not sent (no documented header).
- The response shape follows the documented Azure OpenAI format and is not yet verified
  against the real service.
- Nothing is logged about prompts, responses, keys or Workbench error bodies.
