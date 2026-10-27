from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.db import DatabaseKind, peek_database

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str
    version: str
    # Active database type only (never connection details); null until selected at startup.
    database: DatabaseKind | None = None
    database_fallback: bool = False


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    database = peek_database()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
        version="0.1.0",
        database=database.kind if database else None,
        database_fallback=database.fallback_active if database else False,
    )
