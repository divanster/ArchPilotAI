"""Agent evaluation harness. Fake provider by default; real model calls only with --live.

    python -m app.eval.run --task T-003 --deployment fake-a --deployment fake-b --repeats 3
    python -m app.eval.run --task T-003 --deployment gpt-4o --repeats 2 --live

Every run gets a freshly seeded synthetic Atlas-shaped project in an isolated SQLite file under
the work directory (a new temp folder unless --work-dir is given); the development database is
never opened. Writes results.csv (one row per run) and summary.md (deployments compared).
With --live, each deployment must be the default Workbench deployment or a verified one.
"""

import argparse
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from app.core.config import Settings, get_settings
from app.eval.harness import Checker, ProviderFactory, RunMetrics, default_checker, run_eval
from app.eval.report import markdown_summary, write_csv
from app.eval.seed import make_engine
from app.llm.contracts import LLMProvider
from app.llm.exceptions import LLMConfigurationError
from app.llm.factory import create_provider, deployment_settings, verified_deployments
from app.llm.fake_provider import FakeLLMProvider
from app.llm.workbench_provider import WorkbenchProvider

DB_NAME = "eval.db"
CSV_NAME = "results.csv"
SUMMARY_NAME = "summary.md"


class EvalConfigError(Exception):
    pass


def fake_factory(deployment: str) -> LLMProvider:
    return FakeLLMProvider(model=deployment)


def live_factory(
    settings: Settings, create: Callable[[Settings], LLMProvider] = create_provider
) -> ProviderFactory:
    """Real providers per deployment, on the configured Workbench endpoint."""
    if os.environ.get("CI", "").lower() in {"1", "true"}:
        raise EvalConfigError("Refusing --live in CI")
    if settings.llm_provider != "kpmg_workbench":
        raise EvalConfigError("--live needs ARCHPILOT_LLM_PROVIDER=kpmg_workbench")
    default = settings.workbench_deployment.strip()
    allowed = {default, *verified_deployments(settings)} - {""}
    base: list[LLMProvider] = []

    def factory(deployment: str) -> LLMProvider:
        if deployment not in allowed:
            raise EvalConfigError(
                f"Deployment {deployment!r} is neither the default nor a verified deployment"
            )
        if not base:
            base.append(create(settings))
        provider = base[0]
        if deployment == provider.model_name:
            return provider
        if not isinstance(provider, WorkbenchProvider):
            raise EvalConfigError("Only the Workbench provider can switch deployments")
        budget, effort = deployment_settings(settings, deployment)
        return provider.for_deployment(
            deployment, max_completion_tokens=budget, reasoning_effort=effort
        )

    return factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.eval.run",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--task", required=True, help="plan task key, e.g. T-003")
    parser.add_argument(
        "--deployment",
        action="append",
        required=True,
        help="deployment id; repeat (or comma-separate) to compare several",
    )
    parser.add_argument("--repeats", type=int, default=1, help="runs per deployment (>= 1)")
    parser.add_argument("--max-steps", type=int, default=None, help="cap below the setting")
    parser.add_argument("--work-dir", type=Path, default=None, help="default: a new temp folder")
    parser.add_argument(
        "--live", action="store_true", help="call the real model (default: fake provider)"
    )
    return parser


def _row_line(row: RunMetrics) -> str:
    return (
        f"{row.deployment} #{row.repeat}: {row.status}/{row.stop_reason or row.error} "
        f"steps={row.steps} tokens={row.total_tokens} latency_ms={row.latency_ms}"
    )


def run(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    provider_factory: ProviderFactory | None = None,
    checker: Checker | None = None,
    create: Callable[[Settings], LLMProvider] = create_provider,
) -> int:
    args = _parser().parse_args(argv)
    deployments = list(
        dict.fromkeys(d.strip() for raw in args.deployment for d in raw.split(",") if d.strip())
    )
    if not deployments:
        print("At least one --deployment is required.", file=sys.stderr)
        return 2
    if args.repeats < 1:
        print("--repeats must be at least 1.", file=sys.stderr)
        return 2
    settings = settings or get_settings()
    try:
        if provider_factory is None:
            provider_factory = live_factory(settings, create) if args.live else fake_factory
        elif args.live:
            raise EvalConfigError("--live cannot be combined with an injected provider")
        # Every deployment is resolved before the first run, so a bad one fails up front.
        providers = {d: provider_factory(d) for d in deployments}
    except EvalConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LLMConfigurationError as exc:
        print(f"Configuration error: {exc.public_message}", file=sys.stderr)
        return 2

    work = args.work_dir or Path(tempfile.mkdtemp(prefix="archpilot-eval-"))
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / DB_NAME
    if db_path.exists():
        print(f"{db_path} already exists; choose another --work-dir.", file=sys.stderr)
        return 2
    try:
        engine = make_engine(db_path, settings)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        rows = run_eval(
            engine,
            settings,
            args.task,
            deployments,
            args.repeats,
            providers.__getitem__,
            checker or default_checker(settings),
            max_steps=args.max_steps,
            progress=lambda row: print(_row_line(row), flush=True),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    write_csv(rows, work / CSV_NAME)
    summary = markdown_summary(rows, task_key=args.task, live=args.live, database=db_path)
    (work / SUMMARY_NAME).write_text(summary, encoding="utf-8")
    print(summary)
    print(f"CSV: {work / CSV_NAME}")
    print(f"Summary: {work / SUMMARY_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
