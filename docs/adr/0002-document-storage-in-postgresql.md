# ADR 0002: Store uploaded document text in PostgreSQL

Status: Accepted (MVP)

## Context

Phase 3 accepts UTF-8 `.txt` and `.md` uploads attached to a project. Only text
is needed downstream; no binary formats are parsed yet.

## Decision

Store the extracted text, filename, media type, byte size and SHA-256 hash in a
`documents` table in PostgreSQL. No filesystem or blob storage is used.
Filenames are metadata only and are never used as paths. Content is inert data:
it is never executed, rendered as HTML or treated as instructions. Content is
never logged.

Identical content (same SHA-256) in one project is rejected with 409, enforced
by a unique `(project_id, content_sha256)` constraint; other projects may hold
the same document. Deleting a project deletes its documents.

## Consequences

- Simple transactions, backups and tests with no extra infrastructure.
- Uploads are limited (`ARCHPILOT_MAX_UPLOAD_BYTES`, default 1 MiB) to bound row size.
- The multipart body is spooled by the framework before the size check runs, so a
  reverse proxy must also limit request size in any real deployment.
- No versioning, no binary originals, and large text bloats the database.
- Revisit when PDF/DOCX support arrives: store originals in blob storage and keep
  extracted text and metadata in PostgreSQL.
- The app has no authentication and remains a local-development prototype, not
  safe for public deployment.
