import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


def test_defaults_and_database_url() -> None:
    s = _settings(db_password="placeholder")
    assert s.database_url == "postgresql+psycopg://archpilot:placeholder@localhost:5432/archpilot"


def test_password_is_not_exposed_in_repr() -> None:
    s = _settings(db_password="placeholder")
    assert "placeholder" not in repr(s)


def test_invalid_port_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(db_port=70000)


def test_blank_host_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(db_host="  ")


def test_invalid_environment_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(environment="staging-x")


def test_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHPILOT_DB_HOST", "db")
    assert _settings().db_host == "db"


def test_max_upload_bytes_default_and_validation() -> None:
    assert _settings().max_upload_bytes == 1_048_576
    assert _settings(max_upload_bytes=2048).max_upload_bytes == 2048
    with pytest.raises(ValidationError):
        _settings(max_upload_bytes=0)


def test_extraction_limits_default_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings().max_document_pages == 300
    assert _settings().max_extracted_chars == 1_000_000
    monkeypatch.setenv("ARCHPILOT_MAX_DOCUMENT_PAGES", "12")
    monkeypatch.setenv("ARCHPILOT_MAX_EXTRACTED_CHARS", "5000")
    assert (_settings().max_document_pages, _settings().max_extracted_chars) == (12, 5000)
    for bad in ({"max_document_pages": 0}, {"max_extracted_chars": 10}):
        with pytest.raises(ValidationError):
            _settings(**bad)
