"""`archpilot` command line: parsing, doctor PASS/FAIL and exit codes, start and ui."""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from alembic import command
from app.cli import doctor as doctor_module
from app.cli import main as cli_main
from app.cli.doctor import CheckResult, run_doctor
from app.core.config import Settings
from tests.conftest import alembic_config

runner = CliRunner()


@pytest.fixture(autouse=True)
def plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("ARCHPILOT_REQUIRE_POSTGRES", raising=False)


def sqlite_settings(tmp_path: Path, **extra: Any) -> Settings:
    values: dict[str, Any] = {
        "database_mode": "sqlite",
        "sqlite_path": str(tmp_path / "doctor.db"),
        "llm_provider": "fake",
        "embedding_provider": "none",
        **extra,
    }
    return Settings(_env_file=None, **values)


def ok(name: str) -> Callable[[Settings], CheckResult]:
    def check(settings: Settings) -> CheckResult:
        return CheckResult(name, True, "fine")

    check.__name__ = f"check_{name}"
    return check


def bad(name: str) -> Callable[[Settings], CheckResult]:
    def check(settings: Settings) -> CheckResult:
        return CheckResult(name, False, "broken")

    check.__name__ = f"check_{name}"
    return check


def patch_doctor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    checks: list[Callable[[Settings], CheckResult]],
) -> None:
    monkeypatch.setattr(doctor_module, "load_settings", lambda: sqlite_settings(tmp_path))
    monkeypatch.setattr(doctor_module, "DEFAULT_CHECKS", tuple(checks))


# ---- parsing -----------------------------------------------------------------------------------


def test_help_lists_the_commands() -> None:
    result = runner.invoke(cli_main.cli, ["--help"])
    assert result.exit_code == 0
    for name in ("doctor", "start", "ui", "code"):
        assert name in result.output


def test_code_parses_project_task_resume_theme_and_animation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_tui(*args: Any) -> None:
        seen["args"] = args

    monkeypatch.setattr(cli_main, "run_tui", fake_tui)
    run_id = uuid.uuid4()
    result = runner.invoke(
        cli_main.cli,
        [
            "code",
            "--project",
            "Demo",
            "--task",
            "T1",
            "--resume",
            str(run_id),
            "--theme",
            "amber-terminal",
            "--no-animation",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["args"] == ("Demo", "T1", run_id, "amber-terminal", False)


def test_code_defaults_to_cyber8_with_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli_main, "run_tui", lambda *a: seen.setdefault("args", a))
    assert runner.invoke(cli_main.cli, ["code"]).exit_code == 0
    assert seen["args"] == (None, None, None, "cyber-8", True)


def test_no_color_forces_the_mono_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli_main, "run_tui", lambda *a: seen.setdefault("args", a))
    result = runner.invoke(
        cli_main.cli, ["code", "--theme", "green-phosphor"], env={"NO_COLOR": "1"}
    )
    assert result.exit_code == 0
    assert seen["args"][3] == "mono"


def test_code_refuses_bad_input(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr(cli_main, "run_tui", lambda *a: called.append(a))
    unknown = runner.invoke(cli_main.cli, ["code", "--theme", "rainbow"])
    assert unknown.exit_code == 2 and "Unknown theme" in unknown.output
    assert runner.invoke(cli_main.cli, ["code", "--resume", "not-a-uuid"]).exit_code == 2
    lone = runner.invoke(cli_main.cli, ["code", "--task", "T1"])
    assert lone.exit_code == 2 and "--task needs --project" in lone.output
    assert runner.invoke(cli_main.cli, ["nonsense"]).exit_code == 2
    assert called == []


# ---- doctor ------------------------------------------------------------------------------------


def test_doctor_all_pass_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_doctor(monkeypatch, tmp_path, [ok("git"), ok("providers")])
    result = runner.invoke(cli_main.cli, ["doctor"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("PASS  config")
    assert lines[1].startswith("PASS  git") and lines[2].startswith("PASS  providers")
    assert "all checks passed" in result.output


def test_doctor_any_fail_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_doctor(monkeypatch, tmp_path, [ok("git"), bad("credential store"), ok("providers")])
    result = runner.invoke(cli_main.cli, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL  credential store" in result.output
    assert "PASS  providers" in result.output  # later checks still run


def test_doctor_reports_a_crashing_check_as_fail(tmp_path: Path) -> None:
    def check_boom(settings: Settings) -> CheckResult:
        raise RuntimeError("password=hunter2")

    results = run_doctor(lambda: sqlite_settings(tmp_path), [check_boom])
    assert results[-1] == CheckResult("boom", False, "check crashed (RuntimeError)")
    assert "hunter2" not in results[-1].line()


def test_doctor_invalid_config_names_fields_only(tmp_path: Path) -> None:
    def broken() -> Settings:
        return Settings(_env_file=None, database_mode="bogus", db_password="s3cret")

    results = run_doctor(broken, [ok("git")])
    assert len(results) == 1 and not results[0].ok
    assert "database_mode" in results[0].detail and "s3cret" not in results[0].detail


def test_doctor_database_and_alembic_current_vs_head(tmp_path: Path) -> None:
    settings = sqlite_settings(tmp_path)
    checks = [doctor_module.check_database, doctor_module.check_migrations]
    before = run_doctor(lambda: settings, checks)
    assert [r.ok for r in before] == [True, True, False]
    assert before[1].detail.startswith("sqlite")
    assert "current none" in before[2].detail and "alembic upgrade head" in before[2].detail

    command.upgrade(alembic_config(settings.sqlite_url), "head")
    after = run_doctor(lambda: settings, checks)
    assert all(r.ok for r in after), [r.line() for r in after]
    heads = doctor_module.migration_heads()
    assert all(h in after[2].detail for h in heads)


def test_doctor_providers_fake_passes_and_misconfigured_fails_without_secrets(
    tmp_path: Path,
) -> None:
    fake = doctor_module.check_providers(sqlite_settings(tmp_path))
    assert fake.ok and "llm=fake" in fake.detail
    real = doctor_module.check_providers(
        sqlite_settings(
            tmp_path,
            llm_provider="kpmg_workbench",
            workbench_api_key="sk-VERYSECRET-1234",
            workbench_endpoint="",
        )
    )
    assert not real.ok and "VERYSECRET" not in real.line()


def test_doctor_git_check() -> None:
    result = doctor_module.check_git(Settings(_env_file=None, database_mode="sqlite"))
    assert result.ok == ("git version" in result.detail)


# ---- start and ui ------------------------------------------------------------------------------


class FakeProcess:
    def __init__(self) -> None:
        self.waited = False
        self.terminated = False

    def poll(self) -> int | None:
        return None

    def wait(self) -> int:
        self.waited = True
        return 0

    def terminate(self) -> None:
        self.terminated = True


def test_start_refuses_when_doctor_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_doctor(monkeypatch, tmp_path, [bad("git")])
    spawned: list[Any] = []
    monkeypatch.setattr(cli_main, "spawn_backend", lambda h, p: spawned.append((h, p)))
    monkeypatch.setattr(cli_main, "open_browser", lambda url: spawned.append(url))
    result = runner.invoke(cli_main.cli, ["start"])
    assert result.exit_code == 1 and "FAIL  git" in result.output
    assert spawned == []


def test_start_runs_doctor_starts_backend_and_opens_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_doctor(monkeypatch, tmp_path, [ok("git")])
    process = FakeProcess()
    health = iter([False, False, True])
    opened: list[str] = []
    monkeypatch.setattr(cli_main, "backend_healthy", lambda api: next(health))
    monkeypatch.setattr(cli_main, "spawn_backend", lambda h, p: process)
    monkeypatch.setattr(cli_main, "open_browser", opened.append)
    monkeypatch.setattr(cli_main, "_sleep", lambda s: None)
    result = runner.invoke(cli_main.cli, ["start", "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert "PASS  backend            running at http://127.0.0.1:8123" in result.output
    assert opened == [cli_main.FRONTEND_URL] and process.waited


def test_ui_opens_browser_only_when_backend_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli_main, "open_browser", opened.append)
    monkeypatch.setattr(cli_main, "backend_healthy", lambda api: False)
    down = runner.invoke(cli_main.cli, ["ui"])
    assert down.exit_code == 1 and "FAIL" in down.output and opened == []
    monkeypatch.setattr(cli_main, "backend_healthy", lambda api: True)
    up = runner.invoke(cli_main.cli, ["ui", "--url", "http://localhost:9999"])
    assert up.exit_code == 0 and opened == ["http://localhost:9999"]


def test_entry_point_is_declared() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text("utf-8")
    assert 'archpilot = "app.cli.main:main"' in pyproject
