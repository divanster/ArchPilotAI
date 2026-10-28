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

## Database selection (PostgreSQL / SQLite)

`ARCHPILOT_DATABASE_MODE` selects the database once per process, at startup:

| Mode | Behaviour |
|------|-----------|
| `postgres` (default) | PostgreSQL is required; startup fails with a clear error if unreachable. |
| `sqlite` | Persistent local file at `ARCHPILOT_SQLITE_PATH` (default `backend/data/archpilot.db`). No in-memory database. |
| `auto` | **Local development only** (`ARCHPILOT_ENVIRONMENT=local`). Tries PostgreSQL once (timeout `ARCHPILOT_DB_CONNECT_TIMEOUT_SECONDS`, default 3); if the connection fails, uses SQLite and logs a warning without credentials. |

- The choice never changes after startup. Migration errors, schema mismatches and failed
  queries are reported, never retried on another database.
- `auto` is rejected outside `local`; `production` requires `postgres`; and
  `ARCHPILOT_REQUIRE_POSTGRES=1` (used by PostgreSQL CI tests) rejects `sqlite` and `auto`.
- `GET /health` exposes only the active type (`database`: `sqlite` | `postgresql`) and
  `database_fallback`. The UI shows a development warning when fallback is active.
- **PostgreSQL and SQLite hold separate data.** Nothing is copied, synchronized or migrated
  between them.
- Migrations are never run automatically. Run them explicitly for the database you chose
  (`auto` is refused by Alembic because it must not guess):

```powershell
cd backend
$env:ARCHPILOT_DATABASE_MODE = "sqlite";   alembic upgrade head   # creates/updates the SQLite file
$env:ARCHPILOT_DATABASE_MODE = "postgres"; alembic upgrade head   # uses ARCHPILOT_DB_* settings
```

Example (no secrets; set the password only in your untracked `.env`):

```
ARCHPILOT_DATABASE_MODE=sqlite
ARCHPILOT_SQLITE_PATH=data/archpilot.db
```

SQLite is a development convenience, not a supported production database.

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

## AI Playground and LLM providers

The UI has an AI Playground backed by `POST /api/ai/generate` (local prototype, no
authentication, synthetic/public prompts only, disabled with 403 in production).

- `ARCHPILOT_LLM_PROVIDER=fake` (default): deterministic, offline; used by tests and E2E.
- `ARCHPILOT_LLM_PROVIDER=kpmg_workbench`: set the `ARCHPILOT_WORKBENCH_*` variables listed in
  `.env.example`. Put `ARCHPILOT_WORKBENCH_API_KEY`, `ARCHPILOT_WORKBENCH_CHARGE_CODE` and
  `ARCHPILOT_WORKBENCH_ENDPOINT` in your git-ignored `.env`. Missing values give a clear
  configuration error (HTTP 503); there is no fallback to the fake provider.

Opt-in real smoke test (one request, never run in CI, not run automatically):

```powershell
cd backend
$env:ARCHPILOT_WORKBENCH_SMOKE_TEST = "1"
python -m app.llm.smoke --confirm-authorized-for-archpilotai
```

It prints only status, provider, model, latency and token counts. Run it only with credentials
and a charge code authorized for ArchPilotAI. See `docs/adr/0003-llm-provider-and-workbench.md`.

## Document search (Phase 7A: keyword retrieval only)

Documents can be explicitly indexed into chunks (`para-v1`) and searched by keyword. This is a
lexical baseline, **not** hybrid RAG: no embeddings, no vector search, no LLM calls, and it is not
connected to architecture generation. Nothing is indexed automatically.

- `POST /api/projects/{id}/documents/{doc}/index` (`{"reindex": bool}`), `GET .../index-status`,
  `POST /api/projects/{id}/search` (`query`, optional `document_id`, `top_k` 1-20).
- Chunking: paragraph packing up to `ARCHPILOT_RETRIEVAL_CHUNK_SIZE` (default 1000 chars); long
  paragraphs are split at whitespace with `ARCHPILOT_RETRIEVAL_CHUNK_OVERLAP` (default 150,
  must be <= size/2). Offsets are Python code points into the stored text, CRLF preserved:
  `document.text[source_start:source_end] == chunk.text`. Changing settings marks indexes `stale`
  (still searchable) until reindexed.
- Ranking: project-filtered chunks are scored in Python with BM25 (k1=1.2, b=0.75) over
  NFKC-casefolded `\w+` tokens, ties broken by filename, document id, chunk index. Limitations: no
  stemming, synonyms, phrases or typo tolerance; statistics are per search scope; the scan is
  bounded by `ARCHPILOT_RETRIEVAL_MAX_SCAN_CHUNKS` (default 20000; `scan_limited` is reported).
- Migration (run manually; not run automatically): from `backend`, with your usual database
  settings, `python -m alembic upgrade head` (adds `0005_document_chunks`).

### Embeddings and hybrid retrieval (Phase 7B.1: offline foundation only)

This is **plumbing, not semantic retrieval**. The only embedding provider is a deterministic
`FakeEmbeddingProvider` (hashed word buckets = lexical overlap, no meaning), enabled only with
`ARCHPILOT_EMBEDDING_PROVIDER=fake` (rejected when `ARCHPILOT_ENVIRONMENT=production`). The
default is `none`: vector/hybrid modes are unavailable and keyword search is unchanged. There is
no pgvector and no link to architecture generation. An experimental
`ARCHPILOT_EMBEDDING_PROVIDER=kpmg_workbench` provider exists (needs `ARCHPILOT_EMBEDDING_DEPLOYMENT`
and `ARCHPILOT_EMBEDDING_DIMENSION`), but its hypothesised route returned HTTP 404 in the one
approved probe, so it is unconfirmed (see ADR 0004).

- `POST .../documents/{doc}/embed` (`{"reembed": bool}`), `GET .../embedding-status`,
  `GET /api/projects/{id}/retrieval/capabilities`; `POST .../search` accepts
  `mode` = `keyword` (default) | `vector` | `hybrid` and returns `requested_mode`,
  `effective_mode` (`none` when vector search had nothing to run), `warnings`, and per-hit
  `bm25_rank`/`vector_rank`. Hybrid that cannot use vectors is returned as `keyword` with a
  warning, never as hybrid. Nothing is embedded automatically.
- Storage: portable JSON vectors in `chunk_embeddings` (SQLite and PostgreSQL). An embedding
  space is (provider, model, dimension, embedder version); spaces never mix. A vector is *fresh*
  only if its stored SHA-256 equals the current chunk text hash. Embeddings are cascade-deleted
  with their chunks; **reindexing a document drops its embeddings** (re-embed afterwards).
- Ranking: cosine similarity in Python over project- and space-filtered vectors (bounded by the
  scan limit), scores > 0 only; hybrid uses reciprocal rank fusion (k=60) of the top 20 BM25
  and top 20 vector chunks, deduplicated by chunk id, ties by filename, document id, chunk index.
- Migration (manual): `python -m alembic upgrade head` adds `0006_chunk_embeddings` (and a
  unique `(id, project_id)` constraint on `document_chunks`). Not run automatically.
- Future boundaries: see `docs/adr/0004-embeddings-and-hybrid-retrieval.md`.
## MCP server (Phase 9: read-only, local stdio)

A separate process exposes project knowledge to local AI agents. It only reads, uses the
existing services, listens on no port (stdio only) and has no create/update/delete tools.

```powershell
cd backend
pip install -e .[dev]          # installs the official `mcp` SDK (>=1.2,<2)
python -m app.mcp              # speaks MCP on stdin/stdout; logs go to stderr
```

It uses the same database selection as the API (`backend/.env`). Tools: `list_projects`,
`get_requirements(project_id, status="confirmed")` (each item has `code`, derived from the source
quote when it starts with e.g. `REQ-001:`, else null),
`get_architecture(project_id, version=None, sections=None)` (latest approved by default; without
`sections` it returns the summary, `lint_findings`, status and an index of sections with item
counts; with `sections` only those sections, and `truncated_sections` reports
`{returned, total}` for every cut list), `get_impact(project_id, node_id, mode="direct")`,
`get_orphans(project_id)`, `search_documents(project_id, query, top_k=5)` (BM25). Results carry
source references (document id and offsets, proposal field path) and a `truncated` flag when a
cap was applied. Unknown or malformed ids return a clear error. Each call logs the tool name,
arguments (never document text) and duration.

Connect Claude Code (use the backend virtualenv's Python and an absolute path):

```powershell
claude mcp add archpilotai --cwd C:\path\to\ArchPilotAI\backend -- C:\path\to\ArchPilotAI\backend\.venv\Scripts\python.exe -m app.mcp
```

or in `.mcp.json`:
`{"mcpServers": {"archpilotai": {"command": "C:/path/to/backend/.venv/Scripts/python.exe",
"args": ["-m", "app.mcp"], "cwd": "C:/path/to/backend"}}}`

## Security

See [SECURITY.md](SECURITY.md). Never commit client documents or secrets.
