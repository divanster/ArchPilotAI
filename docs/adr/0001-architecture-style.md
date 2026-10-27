# ADR 0001: Architecture style – modular monolith

- Status: Accepted

## Context

ArchPilotAI is a new product with evolving requirements and a small team. Distributed
services would add operational cost before boundaries are understood.

## Decision

Start as a **modular monolith**: one FastAPI deployable with clear internal modules
(`api`, `core`, `domain`, `models`, `repositories`, `services`, `llm`). Dependencies point
inward: `api` -> `services` -> `repositories`/`domain`. PostgreSQL is the datastore.

All LLM usage, including Workbench later, goes through an `LLMProvider` abstraction so
providers can be swapped and tested without changing business logic.

## Consequences

- Simple local development, testing and deployment.
- Modules can be extracted into services later if justified.
- Module boundaries need discipline since the compiler will not enforce them.
