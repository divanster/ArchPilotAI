"""The built-in hooks of the agent loop (the checks that used to sit inside the tool handlers)."""

from dataclasses import dataclass
from typing import Any

from app.domain.agent import MESSAGES, ActionError, cut
from app.domain.coverage import missing_findings, missing_lines
from app.domain.patch import (
    env_example_has_values,
    has_secret,
    is_text,
    path_problem,
    removed_from_accepted,
)
from app.services.agent_tools import AgentCall, remember_check, static_check_data
from app.services.hooks import ALLOW, Hook, HookRegistry, HookResult, annotate, block

WRITES = frozenset({"write_file", "edit_file"})
CHANGES = frozenset({"write_file", "edit_file", "delete_file"})
FINISH = frozenset({"finish"})


@dataclass
class HookInput:
    call: AgentCall
    tool: str
    args: Any


def proposed_content(ctx: HookInput) -> str | None:
    """The content a write or edit would leave in the file; None when the handler will refuse it
    for its own reasons (unknown file, no or several matches, no change)."""
    a = ctx.args
    if ctx.tool == "write_file":
        return str(a.content)
    file = ctx.call.draft.get(a.path)
    if file is None or file.content.count(a.search) != 1:
        return None
    new = file.content.replace(a.search, a.replace, 1)
    return None if new == file.content else new


def path_rules(ctx: HookInput) -> HookResult:
    problem = path_problem(ctx.args.path)
    content = proposed_content(ctx)
    if problem is None and content is not None:
        if not is_text(content):
            problem = "not_text"
        elif len(content.encode("utf-8")) > ctx.call.svc.limits.max_file_bytes:
            problem = "file_too_large"
    if problem:
        return block(problem, f"Refused: {problem}")
    return ALLOW


def secret_scan(ctx: HookInput) -> HookResult:
    content = proposed_content(ctx)
    if content is not None and has_secret(ctx.args.path, content):
        return block(
            "secret_detected",
            f"Refused: secret detected in {ctx.args.path} (rule: no secrets in file content); "
            "use an environment variable or a placeholder.",
        )
    return ALLOW


def env_example_rule(ctx: HookInput) -> HookResult:
    content = proposed_content(ctx)
    if (
        content is not None
        and ctx.args.path.rsplit("/", 1)[-1] == ".env.example"
        and env_example_has_values(content)
    ):
        return block("env_example_value", "Refused: env_example_value")
    return ALLOW


def static_checks(ctx: HookInput) -> HookResult:
    try:
        data = static_check_data(ctx.call)
    except ActionError as exc:
        # An unavailable checker is told, never reported as "0 findings"; the loop goes on.
        return annotate(cut(str(exc), 200), static_checks=exc.data)
    remember_check(ctx.call, data)
    return annotate(f"{data['total']} finding(s)", static_checks=data)


def patch_validation(ctx: HookInput) -> HookResult:
    failures = ctx.call.svc.draft_failures(ctx.call)
    if failures:
        return block(
            "patch_invalid",
            MESSAGES["patch_invalid"],
            failures=[f.as_json() for f in failures[:50]],
        )
    return ALLOW


def removal_check(ctx: HookInput) -> HookResult:
    call = ctx.call
    accepted = call.svc.accepted_files(call.project_id)
    count = 0
    for change in call.draft.changes():
        if change.change == "modify" and change.path in accepted:
            count += len(removed_from_accepted(change.path, accepted[change.path], change.content))
    if count:
        return annotate(f"{count} definition(s) removed from accepted files; flagged for review.")
    return ALLOW


def coverage_gate(ctx: HookInput) -> HookResult:
    call = ctx.call
    coverage = call.svc.patches.coverage(
        call.project_id, call.task, {p: f.content for p, f in call.draft.live().items()}
    )
    open_items = missing_lines(coverage)
    if not open_items:
        return ALLOW
    if call.run.steps_taken + 1 < call.run.max_steps:
        return block(
            "acceptance_missing",
            f"Finish refused: {len(open_items)} acceptance item(s) name terms the draft "
            "does not define. Add them to the code (names, roles, statuses) and finish again.",
            findings=missing_findings(coverage),
            coverage={k: coverage[k] for k in ("covered", "missing", "unchecked")},
        )
    call.notes += [f"Not covered at the step limit: {line}" for line in open_items]
    return annotate(f"{len(open_items)} acceptance item(s) not covered at the step limit.")


BUILTIN_HOOKS: list[Hook] = [
    Hook(
        "path_rules", "before_action", path_rules, "Path, text and size rules of written files.",
        WRITES, 10, required=True,
    ),
    Hook(
        "secret_scan", "before_action", secret_scan, "Blocks a write with a secret in it.",
        WRITES, 20, required=True,
    ),
    Hook(
        "env_example_rule", "before_action", env_example_rule, ".env.example holds no values.",
        WRITES, 30, required=True,
    ),
    Hook(
        "static_checks", "after_action", static_checks, "Runs the static checks after an edit.",
        CHANGES, 10,
    ),
    Hook(
        "patch_validation", "before_finish", patch_validation, "Validates the draft as a patch.",
        FINISH, 10,
    ),
    Hook(
        "removal_check", "before_finish", removal_check, "Flags definitions removed from files.",
        FINISH, 20,
    ),
    Hook(
        "coverage_gate", "before_finish", coverage_gate, "Acceptance terms the draft defines.",
        FINISH, 30,
    ),
]  # fmt: skip

_registry = HookRegistry(BUILTIN_HOOKS)


def get_hooks() -> HookRegistry:
    return _registry
