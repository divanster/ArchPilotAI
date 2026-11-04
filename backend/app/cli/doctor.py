"""`archpilot doctor`: one PASS/FAIL line per check, read-only, no network calls to providers.

Messages never carry secrets: configuration errors list field names only, database failures
the exception type only, providers their public message.
"""

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import text

from app.core.config import Settings
from app.core.db import DatabaseUnavailableError, select_database
from app.embeddings.factory import create_embedding_provider
from app.llm.exceptions import LLMConfigurationError
from app.llm.factory import create_provider
from app.services.credential_store import KeyringCredentialStore
from app.services.static_checks import REQUIRED_TOOLS, StaticChecker

BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.name:<18} {self.detail}"


Check = Callable[[Settings], CheckResult]


def check_database(settings: Settings) -> CheckResult:
    try:
        db = select_database(settings)
    except DatabaseUnavailableError as exc:
        return CheckResult("database", False, str(exc))
    except Exception as exc:
        return CheckResult("database", False, f"cannot connect ({type(exc).__name__})")
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return CheckResult("database", False, f"cannot connect ({type(exc).__name__})")
    finally:
        db.engine.dispose()
    where = (
        settings.sqlite_path
        if db.kind == "sqlite"
        else f"{settings.db_host}:{settings.db_port}/{settings.db_name}"
    )
    fallback = " (DEVELOPMENT FALLBACK)" if db.fallback_active else ""
    return CheckResult("database", True, f"{db.kind} {where}{fallback}")


def migration_heads() -> set[str]:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


def check_migrations(settings: Settings) -> CheckResult:
    try:
        heads = migration_heads()
        db = select_database(settings)
        try:
            with db.engine.connect() as connection:
                current = set(MigrationContext.configure(connection).get_current_heads())
        finally:
            db.engine.dispose()
    except Exception as exc:
        return CheckResult("alembic", False, f"cannot read revisions ({type(exc).__name__})")
    shown = f"current {', '.join(sorted(current)) or 'none'}, head {', '.join(sorted(heads))}"
    if current == heads:
        return CheckResult("alembic", True, shown)
    return CheckResult("alembic", False, f"{shown}; run: alembic upgrade head")


def check_git(settings: Settings) -> CheckResult:
    git = shutil.which("git")
    if git is None:
        return CheckResult("git", False, "git is not on PATH")
    try:
        out = subprocess.run(
            [git, "--version"], capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("git", False, f"git does not run ({type(exc).__name__})")
    return CheckResult("git", True, out.stdout.strip())


def check_credentials(settings: Settings) -> CheckResult:
    if KeyringCredentialStore().available():
        return CheckResult("credential store", True, "OS keyring available")
    return CheckResult(
        "credential store", False, "no usable OS keyring; a Git PAT cannot be stored"
    )


def check_static(settings: Settings) -> CheckResult:
    status = StaticChecker(
        temp_parent=settings.agent_check_temp_dir,
        timeout=settings.agent_check_timeout_seconds,
        tsc_path=settings.agent_tsc_path,
    ).status()
    missing = [t for t in REQUIRED_TOOLS if not status.get(t, {}).get("available")]
    found = ", ".join(
        f"{name} {info.get('version') or ''}".strip()
        for name, info in status.items()
        if info.get("available")
    )
    if missing:
        return CheckResult("static checkers", False, f"missing: {', '.join(missing)}")
    tsc = "" if status.get("tsc", {}).get("available") else " (tsc not found: optional)"
    return CheckResult("static checkers", True, f"{found}{tsc}")


def check_providers(settings: Settings) -> CheckResult:
    """Builds the configured providers (configuration only; nothing is sent)."""
    try:
        create_provider(settings)
        embeddings = create_embedding_provider(settings)
    except LLMConfigurationError as exc:
        return CheckResult("providers", False, exc.public_message)
    except Exception as exc:
        return CheckResult("providers", False, f"misconfigured ({type(exc).__name__})")
    detail = f"llm={settings.llm_provider} embeddings={settings.embedding_provider}"
    if embeddings is None:
        detail += " (embeddings off)"
    return CheckResult("providers", True, detail)


DEFAULT_CHECKS: Sequence[Check] = (
    check_database,
    check_migrations,
    check_git,
    check_credentials,
    check_static,
    check_providers,
)


def load_settings() -> Settings:
    return Settings()


def run_doctor(
    settings_loader: Callable[[], Settings] | None = None,
    checks: Sequence[Check] | None = None,
) -> list[CheckResult]:
    try:
        settings = (settings_loader or load_settings)()
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "settings" for e in exc.errors()})
        return [CheckResult("config", False, f"invalid settings: {', '.join(fields)}")]
    except Exception as exc:
        return [CheckResult("config", False, f"cannot load settings ({type(exc).__name__})")]
    results = [CheckResult("config", True, f"database mode {settings.database_mode}")]
    for check in DEFAULT_CHECKS if checks is None else checks:
        try:
            results.append(check(settings))
        except Exception as exc:
            name = check.__name__.removeprefix("check_")
            results.append(CheckResult(name, False, f"check crashed ({type(exc).__name__})"))
    return results
