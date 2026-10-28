# ADR 0004: Embedding contract and hybrid retrieval foundation

Status: Accepted (Phase 7B.1, offline foundation; real semantic retrieval is NOT complete)

## Decision

- `EmbeddingProvider` (`app/embeddings/contracts.py`) is separate from `LLMProvider`. `embed(texts)`
  returns vectors, dimension, the actual model when reported, and usage when reported. Identity is
  `provider`, configured `model`, `dimension` and `embedder_version`.
- `FakeEmbeddingProvider` is a labelled test provider. `create_embedding_provider` returns it only
  for `ARCHPILOT_EMBEDDING_PROVIDER=fake` (forbidden in production) and otherwise `None`.
- Vectors are stored as JSON in `chunk_embeddings`, keyed by chunk with a composite FK
  `(chunk_id, project_id)` so a vector cannot reference another project's chunk. Unique per
  (chunk, provider, model, dimension, embedder version).
- Freshness = same space AND stored content hash == SHA-256 of current chunk text. Stale vectors are
  skipped with a warning, never ranked. Different spaces coexist and are never compared.
- Embedding is explicit, idempotent, and atomic: all provider calls and validation (finite numbers,
  non-bool, correct length, nonzero norm, correct count) finish before one transaction replaces the
  document's vectors in that space. Failure keeps the previous vectors.
- Search modes: `keyword`, `vector` (Python cosine), `hybrid` (RRF k=60 over top-20 lists). The
  response states requested and effective mode; fallbacks are explicit.

## Future pgvector adapter boundary

An optional adapter behind the same repository interface (`scan` + `replace`) would replace the
JSON `vector` column with `vector(N)` (or `halfvec`) and push cosine/top-k into SQL, adding an ANN
index (HNSW/IVFFlat; pgvector's indexed-vector limit is 2000 dimensions for `vector`). `CREATE
EXTENSION vector` must be a separate, explicitly approved step; SQLite keeps the Python path.
Server availability and per-database enablement must be verified first (neither is confirmed).

## Workbench prerequisites (not done)

Before a `WorkbenchEmbeddingProvider`: confirmed embedding route (not assumed to match Chat
Completions), API version, request/response shape, required headers, deployment dimension and
input/batch limits, token accounting, and one explicitly approved probe request. Discovery listing
of embedding deployments is not proof of successful embedding inference.

## Limits

Fake vectors capture lexical overlap only. JSON vectors and Python scanning do not scale beyond the
scan limit. Reindexing drops embeddings. No architecture-generation integration.
