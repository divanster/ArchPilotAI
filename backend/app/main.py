import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.ai import router as ai_router
from app.api.architecture import router as architecture_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.projects import router as projects_router
from app.api.requirements import router as requirements_router
from app.api.retrieval import router as retrieval_router
from app.core.config import get_settings
from app.core.db import get_database, reset_database
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_database()  # select the database once, at startup; fails clearly if postgres is required
    yield
    reset_database()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.include_router(health_router)
    app.include_router(projects_router)
    app.include_router(documents_router)
    app.include_router(retrieval_router)
    app.include_router(requirements_router)
    app.include_router(architecture_router)
    app.include_router(ai_router)
    logging.getLogger(__name__).info("application started")
    return app


app = create_app()
