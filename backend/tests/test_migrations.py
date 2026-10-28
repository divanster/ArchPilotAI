import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.conftest import alembic_config


def test_migration_matches_expected_schema(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    columns = {c["name"]: c for c in inspect(engine).get_columns("projects")}
    assert set(columns) == {"id", "name", "description", "status", "created_at", "updated_at"}
    assert columns["name"]["nullable"] is False
    assert columns["description"]["nullable"] is True
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0007_proposal_lint"
        )
    engine.dispose()


def test_downgrade_and_upgrade_roundtrip(postgres_url: str) -> None:
    cfg = alembic_config(postgres_url)
    engine = create_engine(postgres_url)
    try:
        command.downgrade(cfg, "base")
        assert "projects" not in inspect(engine).get_table_names()
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
    check = create_engine(postgres_url)
    assert "projects" in inspect(check).get_table_names()
    check.dispose()


def test_documents_schema_and_constraints(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("documents")}
    assert set(columns) == {
        "id",
        "project_id",
        "original_filename",
        "media_type",
        "size_bytes",
        "content_sha256",
        "extracted_text",
        "created_at",
    }
    assert all(not c["nullable"] for c in columns.values())
    fks = inspector.get_foreign_keys("documents")
    assert fks[0]["referred_table"] == "projects"
    assert fks[0]["options"].get("ondelete") == "CASCADE"
    uniques = inspector.get_unique_constraints("documents")
    assert {tuple(u["column_names"]) for u in uniques} == {
        ("project_id", "content_sha256"),
        ("id", "project_id"),
    }
    engine.dispose()


def test_requirements_schema_and_constraints(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    inspector = inspect(engine)
    req = {c["name"]: c for c in inspector.get_columns("requirements")}
    assert set(req) == {
        "id",
        "project_id",
        "document_id",
        "extraction_run_id",
        "type",
        "title",
        "description",
        "source_quote",
        "source_start",
        "source_end",
        "status",
        "created_at",
        "updated_at",
    }
    assert all(not c["nullable"] for c in req.values())
    runs = {c["name"]: c for c in inspector.get_columns("extraction_runs")}
    assert {
        "provider",
        "model",
        "prompt_version",
        "status",
        "latency_ms",
        "input_tokens",
        "output_tokens",
    } <= set(runs)
    assert runs["input_tokens"]["nullable"] and runs["output_tokens"]["nullable"]
    fks = {fk["name"]: fk for fk in inspector.get_foreign_keys("requirements")}
    composite = fks["fk_requirements_document_project"]
    assert composite["referred_table"] == "documents"
    assert composite["constrained_columns"] == ["document_id", "project_id"]
    checks = {c["name"] for c in inspector.get_check_constraints("requirements")}
    assert {"ck_requirements_type", "ck_requirements_status", "ck_requirements_source_span"} <= (
        checks
    )
    engine.dispose()


def test_requirements_downgrade_and_upgrade_roundtrip(postgres_url: str) -> None:
    cfg = alembic_config(postgres_url)
    engine = create_engine(postgres_url)
    try:
        command.downgrade(cfg, "0002_documents")
        tables = inspect(engine).get_table_names()
        assert "requirements" not in tables
        assert "extraction_runs" not in tables
        assert "documents" in tables
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
    check = create_engine(postgres_url)
    assert "requirements" in inspect(check).get_table_names()
    check.dispose()


def test_downgrade_one_step_keeps_projects(postgres_url: str) -> None:
    cfg = alembic_config(postgres_url)
    engine = create_engine(postgres_url)
    try:
        command.downgrade(cfg, "0001_projects")
        tables = inspect(engine).get_table_names()
        assert "documents" not in tables
        assert "projects" in tables
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()


def test_architecture_schema_and_constraints(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("architecture_proposals")}
    assert set(columns) == {
        "id",
        "project_id",
        "title",
        "summary",
        "status",
        "version",
        "content",
        "provider",
        "model",
        "prompt_version",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "created_at",
        "updated_at",
    }
    assert columns["content"]["nullable"] is False
    fk = inspector.get_foreign_keys("architecture_proposals")[0]
    assert fk["referred_table"] == "projects"
    assert fk["options"].get("ondelete") == "CASCADE"
    uniques = {u["name"] for u in inspector.get_unique_constraints("architecture_proposals")}
    assert "uq_architecture_project_version" in uniques
    checks = {c["name"] for c in inspector.get_check_constraints("architecture_proposals")}
    assert {"ck_architecture_status", "ck_architecture_version"} <= checks

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE architecture_proposals, projects CASCADE"))
        pid = conn.execute(
            text(
                "INSERT INTO projects (id, name, status, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'p', 'draft', now(), now()) RETURNING id"
            )
        ).scalar_one()
    insert = text(
        "INSERT INTO architecture_proposals (id, project_id, title, summary, status, version, "
        "content, provider, model, prompt_version, created_at, updated_at) VALUES "
        "(gen_random_uuid(), :p, 't', 's', :status, :version, '{}', 'fake', 'm', 'v', now(), now())"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"p": pid, "status": "draft", "version": 1})
    for params in (
        {"p": pid, "status": "draft", "version": 1},  # duplicate version
        {"p": pid, "status": "auto_approved", "version": 2},
        {"p": pid, "status": "draft", "version": 0},
        {"p": uuid.uuid4(), "status": "draft", "version": 3},  # unknown project
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, params)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        assert conn.execute(text("SELECT count(*) FROM architecture_proposals")).scalar() == 0
    engine.dispose()


def test_architecture_downgrade_and_upgrade_roundtrip(postgres_url: str) -> None:
    cfg = alembic_config(postgres_url)
    engine = create_engine(postgres_url)
    try:
        command.downgrade(cfg, "0003_requirements")
        tables = inspect(engine).get_table_names()
        assert "architecture_proposals" not in tables
        assert "requirements" in tables
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
    check = create_engine(postgres_url)
    assert "architecture_proposals" in inspect(check).get_table_names()
    check.dispose()


def test_document_chunks_schema_and_constraints(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    inspector = inspect(engine)
    assert "document_chunks" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("document_chunks")}
    assert {
        "id",
        "project_id",
        "document_id",
        "chunk_index",
        "text",
        "source_start",
        "source_end",
        "chunker_version",
    } <= columns
    uniques = {
        u["column_names"] and tuple(u["column_names"])
        for u in inspector.get_unique_constraints("document_chunks")
    }
    assert ("document_id", "chunk_index") in uniques
    engine.dispose()


def test_document_chunks_migration_on_isolated_sqlite_file(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'chunks.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    try:
        assert "document_chunks" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_document_chunks_downgrade_and_upgrade_roundtrip(postgres_url: str) -> None:
    cfg = alembic_config(postgres_url)
    engine = create_engine(postgres_url)
    try:
        command.downgrade(cfg, "0004_architecture")
        tables = inspect(engine).get_table_names()
        assert "document_chunks" not in tables
        assert {"documents", "architecture_proposals"} <= set(tables)
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
    check = create_engine(postgres_url)
    assert "document_chunks" in inspect(check).get_table_names()
    check.dispose()


def test_chunk_embeddings_schema_and_constraints(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("chunk_embeddings")}
    assert {
        "id",
        "project_id",
        "chunk_id",
        "provider",
        "model",
        "actual_model",
        "dimension",
        "embedder_version",
        "content_hash",
        "vector",
        "created_at",
    } <= columns
    uniques = {
        tuple(u["column_names"]) for u in inspector.get_unique_constraints("chunk_embeddings")
    }
    assert ("chunk_id", "provider", "model", "dimension", "embedder_version") in uniques
    engine.dispose()


def test_chunk_embeddings_migration_on_isolated_sqlite_file(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'emb.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        assert "chunk_embeddings" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    command.downgrade(cfg, "0005_document_chunks")
    engine = create_engine(url)
    try:
        tables = inspect(engine).get_table_names()
        assert "chunk_embeddings" not in tables and "document_chunks" in tables
    finally:
        engine.dispose()


def test_chunk_embeddings_downgrade_and_upgrade_roundtrip(postgres_url: str) -> None:
    cfg = alembic_config(postgres_url)
    engine = create_engine(postgres_url)
    try:
        command.downgrade(cfg, "0005_document_chunks")
        tables = inspect(engine).get_table_names()
        assert "chunk_embeddings" not in tables
        assert "document_chunks" in tables
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
    check = create_engine(postgres_url)
    assert "chunk_embeddings" in inspect(check).get_table_names()
    check.dispose()
