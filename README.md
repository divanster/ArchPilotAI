# ArchPilotAI

ArchPilotAI converts project ideas and requirements documents (later: existing repositories,
meetings and communications) into traceable requirements, architecture proposals and
client-ready solution documents.

Status: **early prototype** – health endpoint, project workspace and `.txt`/`.md` document
upload per project (text stored in PostgreSQL, see ADR 0002). No RAG, agents, cloud or
authentication yet; this is a local-development prototype, not safe for public deployment. See [docs/product/mvp-scope.md](docs/product/mvp-scope.md).

## Layout

```
backend/    FastAPI modular monolith (app/{api,core,domain,models,repositories,services,llm})
frontend/   React + TypeScript + Vite
docs/       product, architecture, adr
infra/      reserved for infrastructure (nothing cloud-related yet)
sample-data/ synthetic or public data only
```

## Prerequisites

Python 3.11+ (3.13 used in CI), Node 22+, Docker (optional, for compose).

## Local setup

```powershell
copy .env.example .env      # then set ARCHPILOT_DB_PASSWORD to a local-only value

# Backend
cd backend
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload        # http://localhost:8000/health

# Frontend (second terminal)
cd frontend
npm install
npm run dev                          # http://localhost:5173
```

## Docker Compose

```powershell
docker compose up --build            # PostgreSQL + backend + frontend (UI on :5173)
docker compose config --quiet        # validate (requires ARCHPILOT_DB_PASSWORD set)
```

The database uses the named volume `archpilot_db_data`.

## Commands

| Area     | Command |
|----------|---------|
| Backend tests | `cd backend; pytest` (PostgreSQL tests skip unless `ARCHPILOT_TEST_DATABASE_URL` is set) |
| Backend tests incl. PostgreSQL | see "Testing against PostgreSQL" below |
| Backend lint/format | `ruff check .` / `ruff format .` |
| Backend types | `mypy` |
| Migrations | `alembic upgrade head` (needs a running database) |
| Frontend types | `npm run typecheck` |
| Frontend lint | `npm run lint` |
| Frontend tests | `npm test` |
| Browser E2E | `npm run test:e2e` (see below) |
| Hooks | `pre-commit install` |

## Testing against PostgreSQL and in the browser

Use a **local, empty, throwaway** database whose name ends in `_test`. The test fixtures and
the Playwright config refuse non-local hosts and names not ending in `_test`. Never point
them at a real database.

```powershell
# Backend: migrates the empty DB with Alembic, then runs API/persistence tests on SQLite + PostgreSQL
$env:ARCHPILOT_TEST_DATABASE_URL = "postgresql+psycopg://<user>:<password>@localhost:5432/archpilot_test"
$env:ARCHPILOT_REQUIRE_POSTGRES = "1"   # fail instead of skip when the URL is missing
cd backend; pytest

# E2E: Playwright starts the real backend (alembic upgrade head) on :8100 and the UI on :5183
cd frontend
npx playwright install chromium         # once
$env:ARCHPILOT_DB_HOST = "localhost"; $env:ARCHPILOT_DB_PORT = "5432"
$env:ARCHPILOT_DB_NAME = "archpilot_e2e_test"; $env:ARCHPILOT_DB_USER = "<user>"
$env:ARCHPILOT_DB_PASSWORD = "<password>"
# `python` must resolve to the backend virtualenv; optionally set E2E_BROWSER_CHANNEL=msedge
npm run test:e2e
```

CI runs both against ephemeral PostgreSQL service containers with test-only credentials and
uploads `playwright-report/` and `test-results/` (traces, screenshots) when E2E fails.

## Security

See [SECURITY.md](SECURITY.md). Never commit client documents or secrets.
