from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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

    # Maximum accepted size of an uploaded document, in bytes (default 1 MiB).
    max_upload_bytes: int = Field(default=1_048_576, ge=1, le=50 * 1_048_576)

    @field_validator("db_host", "db_name", "db_user")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

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
