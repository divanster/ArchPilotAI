"""Agent loop protocol and draft-workspace rules (Phase 11B-pre). Pure and static.

The model answers each step with ONE JSON action. Nothing it writes is executed or imported: the
draft is only stored, searched, and (for static checks) parsed by external linters.
Error messages are fixed strings; file content and rejected arguments are never echoed.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.domain.patch import (
    GeneratedFile,
    PatchLimits,
    content_hash,
    env_example_has_values,
    has_secret,
    is_text,
    path_problem,
)

AGENT_PROMPT_VERSION = "agent-v1"
REPEAT_LIMIT = 3  # the same error this many times in a row ends the run
MAX_RESULT_CHARS = 20_000  # stored and sent result, per step
MAX_SUMMARY_CHARS = 300
MAX_LIST_FILES = 200
MAX_SEARCH_MATCHES = 50
MAX_READ_CHARS = 16_000
MAX_FINDINGS = 50
CONVERGENCE_LIMIT = 3  # static checks in a row that do not beat the best finding count
MAX_NOTE_FINDINGS = 10
RESPONSE_CHARS_PER_TOKEN = 8

# Fixed error codes; each has a fixed message below.
MESSAGES = {
    "tests_unavailable": "tests unavailable",
    "unknown_command": "Unknown command id. Allowed: pytest, ruff, mypy, tsc, npm test",
    "output_truncated": "The reply was cut off by the output limit; answer with a shorter action",
    "output_too_large": "The reply is larger than the output limit allows",
    "invalid_json": 'The reply is not one JSON object {"action": "...", "args": {...}}',
    "invalid_shape": 'The reply must be a JSON object with "action" (string) and "args" (object)',
    "unknown_action": "Unknown action",
    "invalid_args": "The arguments do not match the action's schema",
    "file_not_found": "No such file in the draft workspace",
    "file_exists": "A file with that path already exists; use edit_file to change it",
    "no_match": "The search text was not found in the file; read the file and copy it exactly",
    "multiple_matches": "The search text matches more than once; include more surrounding lines",
    "unchanged": "The replacement would not change the file",
    "task_not_found": "No such task in this plan",
    "tool_input": "The request was refused",
    "patch_invalid": "The draft failed validation and was not turned into a patch",
}


class ActionError(Exception):
    """A step the server refuses. Goes back to the model as a result; never raised to the caller."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        path: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.data = data or {}
        super().__init__(message or MESSAGES.get(code, code))


Short = Annotated[str, StringConstraints(max_length=1000)]
ShortList = Annotated[list[Short], Field(max_length=20)]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileArgs(_Args):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ListFilesArgs(_Args):
    prefix: str = Field(default="", max_length=300)
    limit: int = Field(default=100, ge=1, le=MAX_LIST_FILES)


class SearchCodeArgs(_Args):
    query: str = Field(min_length=1, max_length=200)
    prefix: str = Field(default="", max_length=300)


class NoArgs(_Args):
    pass


class GetTaskArgs(_Args):
    key: str | None = Field(default=None, max_length=20)


class GetImpactArgs(_Args):
    node_id: str = Field(max_length=100)
    mode: str = Field(default="direct", max_length=20)


class SearchDocumentsArgs(_Args):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)


class EditFileArgs(_Args):
    path: str
    search: str = Field(min_length=1)
    replace: str


class WriteFileArgs(_Args):
    path: str
    content: str


class DeleteFileArgs(_Args):
    path: str


class RunTestsArgs(_Args):
    command_id: str = Field(max_length=20)  # an allowlisted id, never a command line


class FinishArgs(_Args):
    summary: str = Field(default="", max_length=1000)
    notes: ShortList = Field(default_factory=list)
    assumptions: ShortList = Field(default_factory=list)
    test_suggestions: ShortList = Field(default_factory=list)


ARG_MODELS: dict[str, type[_Args]] = {
    "read_file": ReadFileArgs,
    "list_files": ListFilesArgs,
    "search_code": SearchCodeArgs,
    "get_requirements": NoArgs,
    "get_task": GetTaskArgs,
    "get_impact": GetImpactArgs,
    "search_documents": SearchDocumentsArgs,
    "edit_file": EditFileArgs,
    "write_file": WriteFileArgs,
    "delete_file": DeleteFileArgs,
    "run_static_checks": NoArgs,
    "run_tests": RunTestsArgs,
    "finish": FinishArgs,
}


@dataclass(frozen=True)
class Action:
    name: str
    args: Any  # the validated _Args model of that action


def parse_action(
    raw: str,
    *,
    finish_reason: str | None,
    max_chars: int,
    arg_models: Mapping[str, type[BaseModel]] | None = None,
) -> Action:
    """The one action in a model reply, or an ActionError with a fixed message.

    `arg_models` are the argument models of the tools the caller may use (the registry's)."""
    models = ARG_MODELS if arg_models is None else arg_models
    if finish_reason == "length":
        raise ActionError("output_truncated")
    if len(raw) > max_chars:
        raise ActionError("output_too_large")
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ActionError("invalid_json") from None
    if not isinstance(data, dict) or not isinstance(data.get("action"), str):
        raise ActionError("invalid_shape")
    args = data.get("args", {})
    if not isinstance(args, dict) or set(data) - {"action", "args", "thought"}:
        raise ActionError("invalid_shape")
    name = data["action"]
    model = models.get(name)
    if model is None:
        raise ActionError("unknown_action", f"Unknown action. Use one of: {', '.join(models)}")
    try:
        return Action(name, model.model_validate(args))
    except ValidationError as exc:
        # Locations and error types only: never the offending values.
        problems = [".".join(str(p) for p in e["loc"]) + f": {e['type']}" for e in exc.errors()]
        raise ActionError(
            "invalid_args", f"{MESSAGES['invalid_args']} ({'; '.join(problems[:5])})"
        ) from None


# ---- draft workspace -------------------------------------------------------------------------


@dataclass
class DraftFile:
    path: str
    content: str
    base_hash: str | None  # None: created in this run
    deleted: bool = False


class Draft:
    """The in-memory view of a run's draft. Persisting is the caller's job (see `dirty`)."""

    def __init__(self, files: Sequence[DraftFile]) -> None:
        self.files = {f.path: f for f in files}
        self.dirty: set[str] = set()

    def live(self) -> dict[str, DraftFile]:
        return {p: f for p, f in self.files.items() if not f.deleted}

    def get(self, path: str) -> DraftFile | None:
        found = self.files.get(path)
        return found if found is not None and not found.deleted else None

    def clash(self, path: str) -> str | None:
        folded = path.casefold()
        return next((p for p in self.live() if p.casefold() == folded), None)

    def put(self, path: str, content: str) -> None:
        existing = self.files.get(path)
        if existing is None:
            self.files[path] = DraftFile(path, content, None)
        else:
            existing.content = content
            existing.deleted = False
        self.dirty.add(path)

    def delete(self, path: str) -> None:
        file = self.files[path]
        if file.base_hash is None:
            del self.files[path]  # never existed in the accepted workspace
        else:
            file.deleted = True
        self.dirty.add(path)

    def changes(self) -> list[GeneratedFile]:
        """What this draft would change in the accepted workspace, as a patch."""
        out: list[GeneratedFile] = []
        for path in sorted(self.files):
            file = self.files[path]
            if file.deleted:
                out.append(GeneratedFile(path=path, change="delete"))
            elif file.base_hash is None:
                out.append(GeneratedFile(path=path, change="create", content=file.content))
            elif content_hash(file.content) != file.base_hash:
                out.append(GeneratedFile(path=path, change="modify", content=file.content))
        return out

    def base_hashes(self) -> dict[str, str | None]:
        return {p: f.base_hash for p, f in self.files.items()}


def write_problem(path: str, content: str, limits: PatchLimits) -> str | None:
    """A failure code if this text may not enter the draft at `path`. Syntax is NOT checked here:
    a multi-step edit passes through broken states; finish and the static checks catch syntax."""
    problem = path_problem(path)
    if problem:
        return problem
    if not is_text(content):
        return "not_text"
    if len(content.encode("utf-8")) > limits.max_file_bytes:
        return "file_too_large"
    if has_secret(path, content):
        return "secret_detected"
    if path.rsplit("/", 1)[-1] == ".env.example" and env_example_has_values(content):
        return "env_example_value"
    return None


def search_draft(draft: Draft, query: str, prefix: str) -> tuple[list[dict[str, Any]], int, bool]:
    """Case-insensitive literal search. Returns (matches, total, truncated)."""
    needle = query.casefold()
    matches: list[dict[str, Any]] = []
    total = 0
    for path in sorted(draft.live()):
        if prefix and not path.startswith(prefix):
            continue
        for number, line in enumerate(draft.files[path].content.splitlines(), start=1):
            if needle in line.casefold():
                total += 1
                if len(matches) < MAX_SEARCH_MATCHES:
                    matches.append({"path": path, "line": number, "text": line.strip()[:200]})
    return matches, total, total > len(matches)


# ---- result formatting -----------------------------------------------------------------------


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(frozen=True)
class StepView:
    """What the context builder needs to know about an earlier step."""

    step_no: int
    action: str
    path: str | None
    status: str
    error_code: str | None
    summary: str
    result: str


def step_line(step: StepView) -> str:
    where = f" {step.path}" if step.path else ""
    outcome = "ok" if step.status == "ok" else f"error {step.error_code}"
    return cut(f"{step.step_no}. {step.action}{where} -> {outcome}: {step.summary}", 200)


@dataclass
class ContextInput:
    brief: dict[str, Any]
    steps: Sequence[StepView]
    step_no: int
    max_steps: int
    tokens_used: int
    token_budget: int
    elapsed_seconds: float
    time_limit_seconds: float
    changed: list[dict[str, str]] = field(default_factory=list)
    path_count: int = 0
    decisions: dict[str, Any] = field(default_factory=dict)
    feedback: dict[str, Any] = field(default_factory=dict)


def build_context(ctx: ContextInput, budget_chars: int, last_results: int) -> dict[str, Any]:
    """Task, requirements, a summary of earlier steps and the last N results, within a budget."""
    recent = list(ctx.steps[-last_results:]) if last_results else []
    older = list(ctx.steps[: len(ctx.steps) - len(recent)])
    summary = [step_line(s) for s in older[-40:]]
    base: dict[str, Any] = {
        **ctx.decisions,
        **ctx.feedback,
        "task": ctx.brief,
        "progress": {
            "step": ctx.step_no,
            "max_steps": ctx.max_steps,
            "tokens_left": max(ctx.token_budget - ctx.tokens_used, 0),
            "seconds_left": max(int(ctx.time_limit_seconds - ctx.elapsed_seconds), 0),
        },
        "draft": {"files_total": ctx.path_count, "changed": ctx.changed[:50]},
        "earlier_steps": summary,
        "earlier_steps_omitted": max(len(older) - len(summary), 0),
    }
    fixed = len(json.dumps(base, ensure_ascii=False))
    room = max(budget_chars - fixed, 400)
    per_result = max(room // max(len(recent), 1), 200)
    base["last_results"] = [
        {
            "step": s.step_no,
            "action": s.action,
            "path": s.path,
            "status": s.status,
            "result": cut(s.result, per_result),
        }
        for s in recent
    ]
    while len(json.dumps(base, ensure_ascii=False)) > budget_chars and base["earlier_steps"]:
        base["earlier_steps"] = base["earlier_steps"][len(base["earlier_steps"]) // 2 + 1 :]
        base["earlier_steps_omitted"] = len(older) - len(base["earlier_steps"])
    return base


def dump(result: dict[str, Any]) -> str:
    """The result text sent to the model and stored, bounded."""
    return cut(json.dumps(result, ensure_ascii=False), MAX_RESULT_CHARS)
