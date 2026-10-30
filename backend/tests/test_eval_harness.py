"""The agent evaluation harness, on SQLite with the fake provider only."""

import csv
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings, get_settings
from app.eval import run as cli
from app.eval.harness import CSV_FIELDS, count_removals, run_eval
from app.eval.seed import BASELINE_FILES, make_engine
from app.llm.contracts import LLMProvider, LLMRequest, LLMResponse
from app.llm.fake_provider import FakeLLMProvider
from app.services import agent as agent_service
from app.services.static_checks import CheckReport, Finding

TASK = "T-002"


class FakeChecker:
    """Reports a fixed number of findings; never starts a tool."""

    def __init__(self, total: int = 0) -> None:
        self.total = total
        self.calls = 0

    def run(self, files: Mapping[str, str]) -> CheckReport:
        self.calls += 1
        found = [Finding("ruff", "src/m.py", n + 1, "F401", "unused") for n in range(self.total)]
        return CheckReport(found, self.total, {"ruff": "ok", "mypy": "ok"}, {})


class ReasoningProvider(FakeLLMProvider):
    """The fake provider, but reporting reasoning tokens like a reasoning model would."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        response = super().generate(request)
        usage = response.usage.model_copy(update={"reasoning_tokens": 7})
        return response.model_copy(update={"usage": usage})


def no_real_provider(settings: Settings) -> LLMProvider:
    raise AssertionError("a real provider must not be built")


def action(name: str, **args: Any) -> str:
    return json.dumps({"action": name, "args": args})


@pytest.fixture(autouse=True)
def clean_agent_state() -> Iterator[None]:
    yield
    agent_service._active.clear()
    if agent_service._agent_lock.locked():
        agent_service._agent_lock.release()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Fake provider settings whose development database lives in its own folder."""
    return get_settings().model_copy(
        update={"llm_provider": "fake", "sqlite_path": str(tmp_path / "dev" / "archpilot.db")}
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == CSV_FIELDS
        return list(reader)


def test_default_run_uses_the_fake_provider_and_compares_deployments(
    tmp_path: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "work"
    argv = ["--task", TASK, "--deployment", "dep-a", "--deployment", "dep-b", "--repeats", "2"]
    code = cli.run(
        [*argv, "--work-dir", str(work)],
        settings=settings,
        checker=FakeChecker(),
        create=no_real_provider,
    )
    assert code == 0, capsys.readouterr().err

    rows = read_rows(work / cli.CSV_NAME)
    assert [(r["deployment"], r["repeat"]) for r in rows] == [
        ("dep-a", "1"),
        ("dep-a", "2"),
        ("dep-b", "1"),
        ("dep-b", "2"),
    ]
    for row in rows:
        assert row["provider"] == "fake"
        assert row["model"] == row["deployment"]
        assert row["task_key"] == TASK
        # The default fake agent writes one module that misses FR-001, so the coverage gate
        # refuses every finish until the repeated-error limit stops the run.
        assert (row["status"], row["stop_reason"], row["steps"]) == (
            "stopped",
            "repeated_error",
            "4",
        )
        assert int(row["prompt_tokens"]) > 0 and int(row["completion_tokens"]) > 0
        assert int(row["total_tokens"]) == int(row["prompt_tokens"]) + int(row["completion_tokens"])
        assert row["reasoning_tokens"] == ""  # the fake provider does not report it
        assert int(row["latency_ms"]) >= 0 and int(row["model_latency_ms"]) >= 0
        coverage = [row[f"coverage_{k}"] for k in ("covered", "missing", "unchecked")]
        assert coverage == ["0", "1", "3"]
        assert row["removals_from_accepted"] == "0"
        assert row["static_findings"] == "0"
        assert row["static_unavailable"] == ""
        assert row["patch_id"] == "" and row["error"] == ""
    assert len({r["run_id"] for r in rows}) == 4

    summary = (work / cli.SUMMARY_NAME).read_text(encoding="utf-8")
    assert f"# Agent evaluation: {TASK}" in summary
    assert "fake (no model calls)" in summary
    assert "| dep-a | 2 | 0 | 0 | 4.0 |" in summary
    assert "| dep-b | 2 | 0 | 0 | 4.0 |" in summary
    assert "repeated_error x2" in summary
    # Isolated: the evaluation file exists, the development database was never created.
    assert (work / cli.DB_NAME).is_file()
    assert not Path(settings.sqlite_path).exists()


def test_scripted_run_records_removals_findings_and_reasoning(
    tmp_path: Path, settings: Settings
) -> None:
    shared = BASELINE_FILES["src/atlas/shared.py"]
    script = [
        action(
            "edit_file",
            path="src/atlas/shared.py",
            search=shared[shared.index("def is_eu_region") :],
            replace="",
        ),
        action("delete_file", path="config/atlas.json"),
        action(
            "write_file", path="src/atlas/search.py", content="def search() -> None:\n    pass\n"
        ),
    ]

    def factory(deployment: str) -> LLMProvider:
        return ReasoningProvider(model=deployment, agent_script=list(script))

    checker = FakeChecker(total=3)
    engine = make_engine(tmp_path / "eval.db", settings)
    try:
        rows = run_eval(engine, settings, TASK, ["dep-r"], 1, factory, checker, max_steps=3)
    finally:
        engine.dispose()

    [row] = rows
    assert (row.status, row.stop_reason, row.steps) == ("stopped", "step_limit", 3)
    assert row.reasoning_tokens == 21
    # is_eu_region was edited out of shared.py; the accepted config file was deleted.
    assert (row.removals_from_accepted, row.deleted_accepted_files) == (1, 1)
    assert row.static_findings == 3
    assert checker.calls >= 1
    assert row.patch_id == ""


def test_count_removals_counts_modified_and_deleted_accepted_files() -> None:
    baseline = {"a.py": "def f() -> None:\n    pass\n\n\ndef g() -> None:\n    pass\n"}
    assert count_removals(baseline, dict(baseline)) == (0, 0)
    assert count_removals(baseline, {"a.py": "def f() -> None:\n    pass\n"}) == (1, 0)
    assert count_removals(baseline, {"a.py": None}) == (0, 1)
    assert count_removals(baseline, {}) == (0, 1)


def test_live_is_refused_unless_the_workbench_provider_is_configured(
    tmp_path: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "work"
    argv = ["--task", TASK, "--deployment", "dep-a", "--live", "--work-dir", str(work)]
    assert cli.run(argv, settings=settings, create=no_real_provider) == 2
    assert "kpmg_workbench" in capsys.readouterr().err
    assert not work.exists()


def test_live_is_refused_in_ci(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    workbench = settings.model_copy(
        update={"llm_provider": "kpmg_workbench", "workbench_deployment": "dep-a"}
    )
    argv = ["--task", TASK, "--deployment", "dep-a", "--live", "--work-dir", str(tmp_path / "w")]
    assert cli.run(argv, settings=workbench, create=no_real_provider) == 2


def test_live_only_allows_default_or_verified_deployments(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    workbench = settings.model_copy(
        update={
            "llm_provider": "kpmg_workbench",
            "workbench_deployment": "dep-default",
            "workbench_deployments": {},
        }
    )
    built: list[str] = []

    def create(s: Settings) -> LLMProvider:
        # Stands in for the Workbench provider: no network in tests. The script satisfies the
        # coverage gate, so this run finishes with a proposed patch.
        built.append(s.workbench_deployment)
        content = 'class MicrosoftEntraSignIn:\n    provider = "Microsoft Entra ID"\n'
        script = [
            action("write_file", path="src/identity.py", content=content),
            action("finish", summary="done"),
        ]
        return FakeLLMProvider(model=s.workbench_deployment, agent_script=script)

    refused = tmp_path / "refused"
    argv = ["--task", TASK, "--live", "--work-dir", str(refused)]
    assert cli.run([*argv, "--deployment", "unverified"], settings=workbench, create=create) == 2
    assert built == [] and not refused.exists()

    work = tmp_path / "live"
    argv = ["--task", TASK, "--live", "--deployment", "dep-default", "--work-dir", str(work)]
    assert cli.run(argv, settings=workbench, checker=FakeChecker(), create=create) == 0
    assert built == ["dep-default"]
    assert "live (real model calls)" in (work / cli.SUMMARY_NAME).read_text(encoding="utf-8")
    [row] = read_rows(work / cli.CSV_NAME)
    assert (row["status"], row["stop_reason"], row["steps"]) == ("finished", "finished", "2")
    assert row["patch_id"] and row["coverage_missing"] == "0"


def test_refuses_the_development_database_and_bad_arguments(
    tmp_path: Path, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    dev = Path(settings.sqlite_path)
    with pytest.raises(ValueError, match="development database"):
        make_engine(dev, settings)
    pointed = settings.model_copy(update={"sqlite_path": str(tmp_path / "w" / cli.DB_NAME)})
    base = ["--deployment", "dep-a", "--work-dir", str(tmp_path / "w")]
    assert cli.run(["--task", TASK, *base], settings=pointed, checker=FakeChecker()) == 2
    assert "development database" in capsys.readouterr().err
    assert not (tmp_path / "w" / cli.DB_NAME).exists()

    other = ["--deployment", "dep-a", "--work-dir", str(tmp_path / "x")]
    assert cli.run(["--task", "T-999", *other], settings=settings, checker=FakeChecker()) == 2
    assert "Unknown task T-999" in capsys.readouterr().err
    assert cli.run(["--task", TASK, *other, "--repeats", "0"], settings=settings) == 2
