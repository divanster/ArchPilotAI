# Security Policy

## Secrets

- Never commit secrets, tokens, passwords, keys or connection strings with real credentials.
- Configuration comes from environment variables (`ARCHPILOT_*`). `.env` is git-ignored;
  `.env.example` contains only empty or non-secret placeholders.
- `docker-compose.yml` requires the database password from the environment; no default exists.
- If a secret is committed, rotate it immediately and report it; removing it from history is
  not sufficient.

## Allowed test and sample data

- Only **synthetic** or **publicly available** data may be committed (e.g. `sample-data/`).
- **Client documents, meeting recordings, emails, chat exports and any confidential or
  personal data must never be committed**, including in tests, fixtures, issues or PRs.
- Synthetic data must be clearly labelled as such and contain no real names, addresses or
  identifiers.

## LLM access

All model access goes through the `LLMProvider` abstraction. Do not send client data to any
external service unless explicitly approved.

- Default provider is `fake` (offline). `kpmg_workbench` must be selected explicitly and never
  silently falls back to the fake after a failure.
- The Workbench API key and charge code live only in the git-ignored local `.env`, and only
  credentials provisioned and approved for ArchPilotAI may be used (never another project's).
- Secrets, prompts, authorization headers and model responses are never logged; Workbench error
  bodies are never returned to the frontend. TLS verification is never disabled.
- `POST /api/ai/generate` is a local prototype: synthetic/public prompts only, size and rate
  limited, and it returns 403 when `ARCHPILOT_ENVIRONMENT=production`. It must not be enabled
  anywhere shared without authentication and authorization.
- The real-Workbench smoke command is opt-in, never run in CI, and makes at most one request.

## Authentication

Not implemented yet. The app is a local-development prototype and is **not safe for public
deployment**: anyone who can reach it can read, upload and delete projects and documents.
Do not expose it beyond localhost.

Uploaded documents are treated as inert data: never executed, never rendered as HTML, never
logged, and filenames are metadata only. Upload size is capped by `ARCHPILOT_MAX_UPLOAD_BYTES`
(default 1 MiB); a reverse proxy must also limit request bodies in any real deployment.

## Reporting

Report vulnerabilities privately to the repository owner, not in public issues.
