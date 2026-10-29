"""Patch-only coding agent: parsing and deterministic validation of model-proposed file changes.

Pure and static. Generated code is NEVER executed or imported here: Python is only parsed with
`ast.parse`, YAML only composed (no object construction), JSON and TOML only decoded. Failures are
reported as fixed codes plus the path of the file; rejected content is never echoed.
"""

import ast
import difflib
import hashlib
import json
import re
import tomllib
import warnings
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

PATCH_PROMPT_VERSION = "patch-v1"
RESPONSE_CHARS_PER_TOKEN = 8  # generous ceiling: a longer reply ignored the output cap

CHANGES = ("create", "modify", "delete")
ALLOWED_EXTENSIONS = frozenset(
    {"py", "ts", "tsx", "js", "json", "md", "toml", "yaml", "yml", "css", "html", "sql", "txt"}
)
ALLOWED_DOTFILES = frozenset({".gitignore", ".env.example"})
MAX_PATH_CHARS = 300
MAX_DIFF_LINES = 2000
MAX_REMOVALS = 100

_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{n}" for n in range(1, 10)),
        *(f"LPT{n}" for n in range(1, 10)),
    }
)
_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]*$")
_DRIVE = re.compile(r"^[A-Za-z]:")

# Fixed failure codes (the API contract); a code never carries file content.
FAILURE_CODES = frozenset(
    {
        "empty_patch",
        "too_many_files",
        "patch_too_large",
        "file_too_large",
        "duplicate_path",
        "path_invalid",
        "path_absolute",
        "path_drive",
        "path_traversal",
        "path_hidden",
        "path_reserved",
        "path_conflict",
        "extension_not_allowed",
        "invalid_change",
        "not_text",
        "secret_detected",
        "env_example_value",
        "python_syntax",
        "json_invalid",
        "toml_invalid",
        "yaml_invalid",
        "target_missing",
        "create_exists",
        "stale_previous_hash",
    }
)


@dataclass(frozen=True)
class Failure:
    code: str
    path: str | None = None

    def as_json(self) -> dict[str, str | None]:
        return {"code": self.code, "path": self.path}


@dataclass(frozen=True)
class PatchLimits:
    max_files: int
    max_file_bytes: int
    max_patch_bytes: int


Item = Annotated[str, StringConstraints(max_length=1000)]
ItemList = Annotated[list[Item], Field(max_length=20)]


class GeneratedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    change: str
    content: str = ""


class GeneratedRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(max_length=MAX_PATH_CHARS)
    name: str = Field(max_length=300)
    reason: str = Field(default="", max_length=1000)


class GeneratedPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[GeneratedFile]
    removals: Annotated[list[GeneratedRemoval], Field(max_length=100)] = Field(default_factory=list)
    notes: ItemList = Field(default_factory=list)
    assumptions: ItemList = Field(default_factory=list)
    test_suggestions: ItemList = Field(default_factory=list)


class PatchOutputError(Exception):
    """The model reply is not the strict JSON shape. Messages are fixed; the reply is not echoed."""

    MESSAGES = {
        "output_truncated": "The model reply was cut off by the output limit",
        "output_too_large": "The model reply is larger than the output limit allows",
        "invalid_json": "The model reply is not valid JSON",
        "invalid_schema": "The model reply does not match the required patch schema",
    }

    def __init__(self, code: str, diagnostics: dict[str, Any] | None = None) -> None:
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(self.MESSAGES[code])


class PatchInvalidError(Exception):
    """The proposed patch failed deterministic validation (the whole patch is rejected)."""

    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = list(failures)
        message = "The proposed patch failed validation: " + ", ".join(
            sorted({f.code for f in failures})
        )
        exists = sorted({f.path for f in failures if f.code == "create_exists" and f.path})
        if exists:
            message += (
                ". These paths already exist in the accepted workspace; use modify with the "
                "full new content instead of create: " + ", ".join(exists[:20])
            )
        super().__init__(message)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def convert_creates(
    files: Sequence[GeneratedFile], accepted: Mapping[str, str]
) -> tuple[list[GeneratedFile], list[str]]:
    """Turn "create" of an exact accepted path into "modify" (accepted maps path -> content).

    Only when the previous content hash can be computed from the accepted file; anything else
    (for example a path differing in case) is left for validation to reject.
    """
    out: list[GeneratedFile] = []
    converted: list[str] = []
    for f in files:
        if f.change == "create" and f.path in accepted and content_hash(accepted[f.path]):
            out.append(f.model_copy(update={"change": "modify"}))
            converted.append(f.path)
        else:
            out.append(f)
    return out, converted


def _py_definitions(source: str) -> set[tuple[str, str]]:
    tree = ast.parse(source)
    found: set[tuple[str, str]] = set()

    def walk(body: list[ast.stmt], owner: str | None) -> None:
        for stmt in body:
            prefix = f"{owner}." if owner else ""
            if isinstance(stmt, ast.ClassDef):
                found.add(("class", prefix + stmt.name))
                walk(stmt.body, prefix + stmt.name)
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                found.add(("function", prefix + stmt.name))
            elif owner:
                names: list[str] = []
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names = [stmt.target.id]
                elif isinstance(stmt, ast.Assign):
                    names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                for name in names:
                    found.add(("enum value" if name.isupper() else "field", prefix + name))

    walk(tree.body, None)
    return found


def _keys(value: Any, prefix: str = "") -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            found.add(("key", name))
            found |= _keys(child, name)
    return found


def _structured_keys(extension: str, text: str) -> set[tuple[str, str]]:
    if extension == "json":
        return _keys(json.loads(text))
    if extension == "toml":
        return _keys(tomllib.loads(text))
    documents = list(yaml.load_all(text, Loader=yaml.SafeLoader))
    found: set[tuple[str, str]] = set()
    for document in documents:
        found |= _keys(document)
    return found


def removed_from_accepted(path: str, old: str, new: str) -> list[dict[str, str]]:
    """What a modified file no longer defines compared with its accepted version.

    Python by ast, JSON/YAML/TOML by key paths, other text by lines. Nothing is executed. A side
    that does not parse falls back to the line comparison.
    """
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    gone: list[tuple[str, str]]
    try:
        if extension == "py":
            lost = _py_definitions(old) - _py_definitions(new)
        elif extension in ("json", "toml", "yaml", "yml"):
            lost = _structured_keys(extension, old) - _structured_keys(extension, new)
        else:
            raise ValueError
        order = {"class": 0, "function": 1, "enum value": 2, "field": 3, "key": 4}
        gone = sorted(lost, key=lambda s: (order[s[0]], s[1]))
    except (SyntaxError, ValueError, RecursionError, yaml.YAMLError):
        remaining = [line.strip() for line in new.splitlines() if line.strip()]
        gone = []
        for line in (ln.strip() for ln in old.splitlines()):
            if line and line in remaining:
                remaining.remove(line)
            elif line:
                gone.append(("line", line[:200]))
    return [{"kind": kind, "name": name, "file": path} for kind, name in gone[:MAX_REMOVALS]]


def parse_patch(raw: str, *, finish_reason: str | None, max_chars: int) -> GeneratedPatch:
    if finish_reason == "length":
        raise PatchOutputError("output_truncated")
    if len(raw) > max_chars:
        raise PatchOutputError("output_too_large", {"raw_chars": len(raw)})
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise PatchOutputError("invalid_json", {"raw_chars": len(raw)}) from None
    try:
        return GeneratedPatch.model_validate(data)
    except ValidationError as exc:
        # Only schema locations and error types: never the offending values.
        problems = [
            {"path": ".".join(str(p) for p in e["loc"]), "type": e["type"]} for e in exc.errors()
        ]
        raise PatchOutputError("invalid_schema", {"problems": problems[:20]}) from None


# ---- paths -----------------------------------------------------------------------------------


def path_problem(raw: str) -> str | None:
    """The failure code for an unacceptable relative path, or None."""
    if not raw or len(raw) > MAX_PATH_CHARS:
        return "path_invalid"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return "path_invalid"
    if raw.startswith(("/", "\\")):
        return "path_absolute"
    if _DRIVE.match(raw):
        return "path_drive"
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    if ".." in parts:
        return "path_traversal"
    if "\\" in raw or ":" in raw:
        return "path_invalid"
    if any(p in ("", ".") for p in parts):
        return "path_invalid"
    last = parts[-1]
    for index, part in enumerate(parts):
        if part.startswith(".") and not (index == len(parts) - 1 and part in ALLOWED_DOTFILES):
            return "path_hidden"
        if not _PART.match(part) and part not in ALLOWED_DOTFILES:
            return "path_invalid"
        if part.endswith("."):
            return "path_invalid"
        if part.split(".", 1)[0].upper() in _RESERVED_NAMES:
            return "path_reserved"
    if last in ALLOWED_DOTFILES:
        return None
    extension = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    if extension not in ALLOWED_EXTENSIONS:
        return "extension_not_allowed"
    return None


# ---- secrets ---------------------------------------------------------------------------------

_NAME = (
    r"[A-Za-z0-9_-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|private[_-]?key"
    r"|access[_-]?key|credential)[A-Za-z0-9_-]*"
)
_TOKEN_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
_QUOTED_ASSIGNMENT = re.compile(rf"""(?i)\b{_NAME}["']?\s*[:=]\s*["']([^"'\s]{{8,}})["']""")
_ENV_ASSIGNMENT = re.compile(rf"(?im)^\s*(?:export\s+)?{_NAME}\s*[:=]\s*([^\s\"'#]{{8,}})")
_CONNECTION = re.compile(r"""\b[a-z][a-z0-9+.-]{1,20}://([^\s/:@"'<>]+):([^\s/@"'<>]+)@""", re.I)
_PLACEHOLDER_WORDS = frozenset({"password", "pass", "changeme", "secret", "example", "user"})
_CODE_EXTENSIONS = frozenset({"py", "ts", "tsx", "js", "sql", "css", "html"})


def _placeholder(value: str) -> bool:
    return (
        value.startswith(("<", "${", "{{", "%(", "$"))
        or value.lower() in _PLACEHOLDER_WORDS
        or len(set(value)) == 1
    )


def has_secret(path: str, content: str) -> bool:
    """True when the text looks like it holds a credential. Never returns what matched."""
    if any(p.search(content) for p in _TOKEN_PATTERNS):
        return True
    if any(not _placeholder(m.group(1)) for m in _QUOTED_ASSIGNMENT.finditer(content)):
        return True
    if any(not _placeholder(m.group(2)) for m in _CONNECTION.finditer(content)):
        return True
    extension = path.rsplit(".", 1)[-1].lower()
    if extension not in _CODE_EXTENSIONS:
        for m in _ENV_ASSIGNMENT.finditer(content):
            value = m.group(1)
            if (
                not _placeholder(value)
                and re.search(r"\d", value)
                and re.search(r"[A-Za-z]", value)
            ):
                return True
    return False


_ENV_EXAMPLE_LINE = re.compile(r"""^[A-Za-z_][A-Za-z0-9_]*=(""|'')?$""")


def env_example_has_values(content: str) -> bool:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not _ENV_EXAMPLE_LINE.match(stripped):
            return True
    return False


# ---- content ---------------------------------------------------------------------------------

_ALLOWED_CONTROL = frozenset("\t\n\r\f")


def is_text(content: str) -> bool:
    """UTF-8 encodable and free of NUL / control characters (a binary file in disguise)."""
    if any(ord(ch) < 32 and ch not in _ALLOWED_CONTROL for ch in content) or "\x7f" in content:
        return False
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def syntax_problem(path: str, content: str) -> str | None:
    """Parse only; nothing is run. Returns a failure code or None."""
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    try:
        if extension == "py":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ast.parse(content, filename="<patch>", mode="exec")
        elif extension == "json":
            json.loads(content)
        elif extension == "toml":
            tomllib.loads(content)
        elif extension in ("yaml", "yml"):
            # compose builds the node graph only: no tag is resolved to an object, no code runs.
            list(yaml.compose_all(content, Loader=yaml.SafeLoader))
    except (SyntaxError, ValueError, RecursionError, MemoryError, tomllib.TOMLDecodeError):
        return {
            "py": "python_syntax",
            "json": "json_invalid",
            "toml": "toml_invalid",
        }.get(extension, "yaml_invalid")
    except yaml.YAMLError:
        return "yaml_invalid"
    return None


def validate_patch(
    files: Sequence[GeneratedFile],
    workspace_hashes: Mapping[str, str],
    limits: PatchLimits,
) -> list[Failure]:
    """Every rule that can be decided from the patch and the accepted workspace. Empty = valid."""
    if not files:
        return [Failure("empty_patch")]
    if len(files) > limits.max_files:
        return [Failure("too_many_files")]
    failures: list[Failure] = []
    seen: dict[str, str] = {}
    total = 0
    for file in files:
        path = file.path
        shown = path if len(path) <= MAX_PATH_CHARS else path[:MAX_PATH_CHARS]
        shown = "".join(ch if 32 <= ord(ch) != 127 else "?" for ch in shown)
        problem = path_problem(path)
        if problem:
            failures.append(Failure(problem, shown))
            continue
        if file.change not in CHANGES:
            failures.append(Failure("invalid_change", shown))
            continue
        folded = path.casefold()
        if folded in seen:
            failures.append(Failure("duplicate_path", path))
            continue
        seen[folded] = path
        failures.extend(_content_failures(file, limits))
        if file.change != "delete":
            total += len(file.content.encode("utf-8", errors="ignore"))
    failures.extend(_conflicts(files, workspace_hashes))
    if total > limits.max_patch_bytes:
        failures.append(Failure("patch_too_large"))
    return failures


def _content_failures(file: GeneratedFile, limits: PatchLimits) -> list[Failure]:
    if file.change == "delete":
        return []
    path, content = file.path, file.content
    if not is_text(content):
        return [Failure("not_text", path)]
    if len(content.encode("utf-8")) > limits.max_file_bytes:
        return [Failure("file_too_large", path)]
    found: list[Failure] = []
    if has_secret(path, content):
        found.append(Failure("secret_detected", path))
    if path.rsplit("/", 1)[-1] == ".env.example" and env_example_has_values(content):
        found.append(Failure("env_example_value", path))
    code = syntax_problem(path, content)
    if code:
        found.append(Failure(code, path))
    return found


def _conflicts(
    files: Sequence[GeneratedFile], workspace_hashes: Mapping[str, str]
) -> list[Failure]:
    """Existence rules against the accepted workspace, and file/directory clashes."""
    failures: list[Failure] = []
    existing = {p.casefold(): p for p in workspace_hashes}
    deleted = {f.path.casefold() for f in files if f.change == "delete"}
    for file in files:
        if path_problem(file.path) or file.change not in CHANGES:
            continue
        if file.change in ("modify", "delete") and file.path not in workspace_hashes:
            failures.append(Failure("target_missing", file.path))
        if file.change == "create" and file.path.casefold() in existing:
            failures.append(Failure("create_exists", file.path))
    final = {p for p in existing if p not in deleted}
    final |= {f.path.casefold() for f in files if f.change != "delete" and not path_problem(f.path)}
    for path in sorted(final):
        parts = path.split("/")
        if any("/".join(parts[:n]) in final for n in range(1, len(parts))):
            failures.append(Failure("path_conflict", path))
    return failures


def stale_failures(
    changes: Collection[tuple[str, str, str | None]], workspace_hashes: Mapping[str, str]
) -> list[Failure]:
    """Re-check a stored patch (path, change, previous hash) against the CURRENT workspace."""
    failures: list[Failure] = []
    folded = {p.casefold() for p in workspace_hashes}
    for path, change, previous in sorted(changes):
        if change == "create":
            if path.casefold() in folded:
                failures.append(Failure("create_exists", path))
        elif path not in workspace_hashes:
            failures.append(Failure("target_missing", path))
        elif workspace_hashes[path] != previous:
            failures.append(Failure("stale_previous_hash", path))
    return failures


def unified_diff(path: str, change: str, old: str | None, new: str) -> tuple[str, bool]:
    """Unified diff of one file, cut at MAX_DIFF_LINES. Returns (text, truncated)."""
    before = [] if change == "create" or old is None else old.splitlines(keepends=True)
    after = [] if change == "delete" else new.splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            before,
            after,
            fromfile="/dev/null" if change == "create" else f"a/{path}",
            tofile="/dev/null" if change == "delete" else f"b/{path}",
        )
    )
    cut = len(lines) > MAX_DIFF_LINES
    text = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in lines[:MAX_DIFF_LINES]
    )
    return text, cut


def task_status(patch_statuses: Collection[str]) -> str:
    """todo / proposed / accepted for one task, from the statuses of all its patches."""
    if "proposed" in patch_statuses:
        return "proposed"
    if "accepted" in patch_statuses:
        return "accepted"
    return "todo"


def requirement_code(quote: str) -> str | None:
    match = re.match(r"^([A-Z]{2,4}-\d{3}):", quote)
    return match.group(1) if match else None
