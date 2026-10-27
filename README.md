# ArchPilotAI

ArchPilotAI converts project ideas and requirements documents (later: existing repositories,
meetings and communications) into traceable requirements, architecture proposals and
client-ready solution documents.

Status: **foundation only** – a health endpoint, a minimal UI and tooling. No RAG, agents,
cloud or authentication yet. See [docs/product/mvp-scope.md](docs/product/mvp-scope.md).

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
| Backend tests | `cd backend; pytest` |
| Backend lint/format | `ruff check .` / `ruff format .` |
| Backend types | `mypy` |
| Migrations | `alembic upgrade head` (needs a running database) |
| Frontend types | `npm run typecheck` |
| Frontend lint | `npm run lint` |
| Frontend tests | `npm test` |
| Hooks | `pre-commit install` |

## Security

See [SECURITY.md](SECURITY.md). Never commit client documents or secrets.
