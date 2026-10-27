# MVP Scope

## Foundation (current step)

- Repository structure, FastAPI backend with `GET /health`, React/Vite frontend page.
- PostgreSQL configuration, SQLAlchemy 2, Alembic (no domain tables yet).
- Docker Compose, CI, linting, type-checking, tests.

## Next (planned, not started)

- Ingest requirements documents and project ideas (synthetic/public data only).
- Extract traceable requirements.
- Generate architecture proposals and solution documents.
- `LLMProvider` implementations (Workbench later).

## Out of scope for now

RAG, embeddings, pgvector, Neo4j, GraphRAG, MCP, agents, Azure/cloud, email/Teams
integrations, Workbench calls, authentication, repository/meeting analysis.
