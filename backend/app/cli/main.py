"""The `archpilot` command: doctor, start, ui and code (the retro TUI)."""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from typing import Annotated

import typer

from app.cli import doctor as doctor_module
from app.tui.theme import THEMES, UnknownThemeError, resolve_theme

API_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://localhost:5173"
START_TIMEOUT_SECONDS = 30.0

cli = typer.Typer(
    name="archpilot",
    help="ArchPilotAI: doctor, start the backend, open the web UI, or code in the terminal.",
    no_args_is_help=True,
    add_completion=False,
)


# ---- replaceable side effects (tests swap these) -----------------------------------------------


def backend_healthy(api_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=2) as response:  # noqa: S310
            return int(response.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def open_browser(url: str) -> None:
    webbrowser.open(url)


def spawn_backend(host: str, port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
        cwd=doctor_module.BACKEND_DIR,
    )


def run_tui(
    project: str | None,
    task: str | None,
    resume: uuid.UUID | None,
    theme: str,
    animation: bool,
    change: str | None = None,
) -> None:
    from app.tui.app import ArchPilotApp
    from app.tui.backend import Backend

    ArchPilotApp(
        Backend.from_settings(),
        project=project,
        task=task,
        resume=resume,
        theme=theme,
        animation=animation,
        change=change,
    ).run()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---- commands ----------------------------------------------------------------------------------


def _doctor() -> bool:
    results = doctor_module.run_doctor()
    for result in results:
        typer.echo(result.line())
    ok = all(r.ok for r in results)
    typer.echo("doctor: all checks passed" if ok else "doctor: some checks FAILED")
    return ok


@cli.command()
def doctor() -> None:
    """Check config, database, migrations, Git, credential store, static checkers, providers."""
    if not _doctor():
        raise typer.Exit(code=1)


@cli.command()
def start(
    host: Annotated[str, typer.Option(help="Backend host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Backend port.")] = 8000,
    url: Annotated[str, typer.Option(help="Web UI to open.")] = FRONTEND_URL,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open it.")] = False,
) -> None:
    """Run doctor, start the backend and open the browser."""
    if not _doctor():
        typer.echo("FAIL  start              fix the failed checks first")
        raise typer.Exit(code=1)
    api = f"http://{host}:{port}"
    if backend_healthy(api):
        typer.echo(f"PASS  backend            already running at {api}")
        process = None
    else:
        process = spawn_backend(host, port)
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while not backend_healthy(api):
            if process.poll() is not None or time.monotonic() > deadline:
                process.terminate()
                typer.echo(f"FAIL  backend            did not start at {api}")
                raise typer.Exit(code=1)
            _sleep(0.5)
        typer.echo(f"PASS  backend            running at {api}")
    if not no_browser:
        open_browser(url)
        typer.echo(f"      browser            {url}")
    if process is not None:
        try:
            process.wait()
        except KeyboardInterrupt:
            process.terminate()


@cli.command()
def ui(
    api: Annotated[str, typer.Option(help="Backend URL.")] = API_URL,
    url: Annotated[str, typer.Option(help="Web UI to open.")] = FRONTEND_URL,
) -> None:
    """Open the browser if the backend already runs."""
    if not backend_healthy(api):
        typer.echo(f"FAIL  backend            not running at {api}; use: archpilot start")
        raise typer.Exit(code=1)
    open_browser(url)
    typer.echo(f"PASS  backend            running at {api}; opened {url}")


@cli.command()
def code(
    project: Annotated[str | None, typer.Option(help="Project id or name.")] = None,
    task: Annotated[str | None, typer.Option(help="Task key of the approved plan.")] = None,
    resume: Annotated[uuid.UUID | None, typer.Option(help="Agent run id to open.")] = None,
    change: Annotated[
        str | None, typer.Option(help="Free-form change request text (needs --project).")
    ] = None,
    theme: Annotated[
        str | None, typer.Option(help=f"Theme: {', '.join(THEMES)}. NO_COLOR forces mono.")
    ] = None,
    no_animation: Annotated[
        bool, typer.Option("--no-animation", help="No blinking cursor or animation.")
    ] = False,
) -> None:
    """Open the retro terminal interface."""
    try:
        chosen = resolve_theme(theme, os.environ)
    except UnknownThemeError as exc:
        typer.echo(f"FAIL  theme              {exc}", err=True)
        raise typer.Exit(code=2) from None
    if task is not None and project is None and resume is None:
        typer.echo("FAIL  task               --task needs --project", err=True)
        raise typer.Exit(code=2)
    if change is not None and project is None:
        typer.echo("FAIL  change            --change needs --project", err=True)
        raise typer.Exit(code=2)
    if change is None:
        run_tui(project, task, resume, chosen, not no_animation)
    else:
        run_tui(project, task, resume, chosen, not no_animation, change=change)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
