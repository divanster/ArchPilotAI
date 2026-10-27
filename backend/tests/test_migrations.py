from sqlalchemy import create_engine, inspect, text

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
            "0002_documents"
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
    assert {tuple(u["column_names"]) for u in uniques} == {("project_id", "content_sha256")}
    engine.dispose()


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
