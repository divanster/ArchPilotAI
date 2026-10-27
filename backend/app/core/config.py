from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ARCHPILOT_", extra="ignore")

    app_name: str = "ArchPilotAI"
    environment: Literal["local", "test", "ci", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    db_host: str = "localhost"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "archpilot"
    db_user: str = "archpilot"
    db_password: SecretStr = SecretStr("")

    # sqlite: local file; postgres: required (default, fail closed); auto: local development only.
    database_mode: Literal["sqlite", "postgres", "auto"] = "postgres"
    sqlite_path: str = "data/archpilot.db"
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    # Same variable the PostgreSQL-required test setup already uses.
    require_postgres: bool = False

    # LLM access (see ADR 0003). Default is the offline fake provider.
    llm_provider: Literal["fake", "kpmg_workbench"] = "fake"
    workbench_api_key: SecretStr = SecretStr("")
    workbench_auth_type: Literal["subscription_key"] = "subscription_key"
    workbench_endpoint: str = ""
    workbench_deployment: str = ""
    workbench_api_version: str = ""
    workbench_region: str = ""
    workbench_charge_code: str = ""
    # Accepted for configuration compatibility; the documented contract has no header for it.
    workbench_approval_group: str = ""
    workbench_proxy: str = ""
    workbench_timeout: float = Field(default=90.0, gt=0, le=300)
    workbench_max_retries: int = Field(default=2, ge=0, le=5)

    # Local-prototype guard rails for POST /api/ai/generate.
    ai_max_prompt_chars: int = Field(default=4000, ge=1, le=20_000)
    ai_max_output_tokens: int = Field(default=400, ge=1, le=4000)
    ai_min_interval_seconds: float = Field(default=2.0, ge=0, le=60)

    # Maximum accepted size of an uploaded document, in bytes (default 1 MiB).
    max_upload_bytes: int = Field(default=1_048_576, ge=1, le=50 * 1_048_576)

    @field_validator("db_host", "db_name", "db_user")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("sqlite_path")
    @classmethod
    def _sqlite_path_is_file(cls, value: str) -> str:
        if not value.strip() or value.strip() == ":memory:":
            raise ValueError("must be a file path (in-memory SQLite is not supported)")
        return value

    @model_validator(mode="after")
    def _database_mode_allowed(self) -> "Settings":
        if self.database_mode == "auto" and self.environment != "local":
            raise ValueError("database_mode=auto is only allowed when environment=local")
        if self.environment == "production" and self.database_mode != "postgres":
            raise ValueError("production requires database_mode=postgres")
        if self.require_postgres and self.database_mode != "postgres":
            raise ValueError("PostgreSQL is required (ARCHPILOT_REQUIRE_POSTGRES=1)")
        return self

    @property
    def sqlite_url(self) -> str:
        return f"sqlite+pysqlite:///{Path(self.sqlite_path).as_posix()}"

    @property
    def migration_url(self) -> str:
        """URL for Alembic; never falls back, so `auto` must be resolved explicitly."""
        if self.database_mode == "sqlite":
            return self.sqlite_url
        if self.database_mode == "postgres":
            return self.database_url
        raise ValueError(
            "Alembic needs an explicit database: set ARCHPILOT_DATABASE_MODE=sqlite or postgres"
        )

    @property
    def database_url(self) -> str:
        password = self.db_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
