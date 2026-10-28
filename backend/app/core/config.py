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
    # Reasoning deployments only (GPT-5 / o-series); empty keeps the model default.
    workbench_reasoning_effort: Literal["", "none", "low", "medium", "high"] = ""

    # Local-prototype guard rails for POST /api/ai/generate.
    ai_max_prompt_chars: int = Field(default=4000, ge=1, le=20_000)
    ai_max_output_tokens: int = Field(default=400, ge=1, le=4000)
    ai_min_interval_seconds: float = Field(default=2.0, ge=0, le=60)

    # Requirements extraction. Sending documents to a real provider is explicit opt-in only.
    extraction_allow_real_provider: bool = False
    extraction_max_document_chars: int = Field(default=12_000, ge=1, le=100_000)
    extraction_max_output_tokens: int = Field(default=2000, ge=1, le=8000)

    # Architecture generation. Reuses the extraction opt-in for real providers.
    architecture_max_requirements: int = Field(default=60, ge=1, le=200)
    architecture_max_context_chars: int = Field(default=24_000, ge=1, le=100_000)
    architecture_max_unconfirmed_chars: int = Field(default=4_000, ge=0, le=20_000)
    # Retrieval-grounded context (Phase 7C): the char budget above caps the passages (0 = none).
    architecture_passages_per_requirement: int = Field(default=3, ge=1, le=10)
    architecture_max_passages: int = Field(default=12, ge=1, le=50)
    architecture_max_output_tokens: int = Field(default=8000, ge=1, le=8000)
    # Extra product / service names for the origin lint (added to the built-in list).
    lint_extra_product_terms: list[str] = Field(default_factory=list)
    # Graph impact: a node touching more edges than this is a hub and is not expanded.
    graph_hub_threshold: int = Field(default=5, ge=1, le=100)

    # Document chunking and keyword retrieval (Phase 7A). Overlap must be at most size // 2.
    retrieval_chunk_size: int = Field(default=1000, ge=100, le=8000)
    retrieval_chunk_overlap: int = Field(default=150, ge=0, le=4000)
    retrieval_max_scan_chunks: int = Field(default=20_000, ge=1, le=200_000)

    # "fake" is a deterministic lexical test provider; "none" disables vector search.
    embedding_provider: Literal["none", "fake", "kpmg_workbench"] = "none"
    # Workbench embeddings reuse the workbench_* endpoint, key, region, proxy and timeout.
    embedding_deployment: str = ""
    # Must match the deployment's real output size; 0 = not configured (provider is refused).
    embedding_dimension: int = Field(default=0, ge=0, le=16_384)
    embedding_batch_size: int = Field(default=16, ge=1, le=2048)

    # Patch-only coding agent (Phase 11A). Generated code lives in the database; a folder is only
    # written on an explicit export, and must be OUTSIDE the ArchPilotAI repository.
    workspace_root: str = ""
    patch_max_output_tokens: int = Field(default=8000, ge=1, le=8000)
    patch_max_files: int = Field(default=20, ge=1, le=100)
    patch_max_file_bytes: int = Field(default=100_000, ge=1, le=1_000_000)
    patch_max_patch_bytes: int = Field(default=400_000, ge=1, le=5_000_000)
    patch_max_context_chars: int = Field(default=60_000, ge=1_000, le=200_000)

    # Agent loop with static feedback (Phase 11B-pre). The loop speaks a JSON protocol; native
    # tool calling is not used. Static checks run only ruff, mypy and tsc on a temporary copy.
    agent_max_steps: int = Field(default=20, ge=1, le=50)
    agent_token_budget: int = Field(default=150_000, ge=1, le=2_000_000)
    agent_time_limit_seconds: float = Field(default=300.0, gt=0, le=3600)
    agent_step_max_output_tokens: int = Field(default=4000, ge=1, le=8000)
    agent_context_chars: int = Field(default=24_000, ge=2_000, le=100_000)
    agent_context_last_results: int = Field(default=3, ge=1, le=10)
    agent_check_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    # Parent of the temporary check directories; empty uses the system temporary folder.
    agent_check_temp_dir: str = ""
    # Path to the TypeScript compiler; empty auto-detects the frontend's local install or PATH.
    agent_tsc_path: str = ""

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
    def _chunk_overlap_progresses(self) -> "Settings":
        if self.retrieval_chunk_overlap > self.retrieval_chunk_size // 2:
            raise ValueError("retrieval_chunk_overlap must be at most retrieval_chunk_size // 2")
        return self

    @model_validator(mode="after")
    def _no_fake_embeddings_in_production(self) -> "Settings":
        if self.environment == "production" and self.embedding_provider == "fake":
            raise ValueError("the fake embedding provider is not allowed in production")
        return self

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
