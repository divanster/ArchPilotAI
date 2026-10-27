# System Context

```
 Architect (browser)
        |
        v
 Frontend (React/Vite)  --HTTP-->  Backend (FastAPI modular monolith)  --SQL-->  PostgreSQL
                                          |
                                          +--LLMProvider --> Workbench
```

## Backend modules

| Package | Responsibility |
|---------|----------------|
| `api` | HTTP routers and request/response schemas |
| `core` | Settings, logging, database wiring |
| `domain` | Business entities and rules, framework-free |
| `models` | SQLAlchemy ORM models |
| `repositories` | Persistence access |
| `services` | Use-case orchestration |
| `llm` | `LLMProvider` abstraction, fake and KPMG Workbench providers, factory (ADR 0003) |

## Constraints

- Single deployable backend (modular monolith); see ADR 0001.
- No authentication or cloud infrastructure yet.
- Only synthetic/public data; see SECURITY.md.
