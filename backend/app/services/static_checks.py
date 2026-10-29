"""Static checks of a draft workspace: ruff, mypy, tsc --noEmit and parsers. Nothing is executed.

The draft is written to a private temporary directory that is always removed. The tools run with a
scrubbed environment, a fixed configuration owned by ArchPilotAI (configuration files inside the
draft are ignored: ruff `--isolated`, mypy `--config-file`, tsc given explicit files), no network
(proxy variables point nowhere; none of the tools needs one), a timeout, and from a working
directory outside the draft. Python is run with `-I` so no draft file can shadow a module.
Tests, scripts, installs and package managers are never run.
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import warnings
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from app.domain.agent import MAX_FINDINGS
from app.domain.patch import path_problem
from app.services.workspace_export import REPO_ROOT

MAX_MESSAGE_CHARS = 300
# E9 (syntax/IO errors) and all F rules (undefined names, unused imports...). Style rules (E, B) are
# not actionable for the model: B008 even flags the normal FastAPI `Depends(...)` default.
RUFF_RULES = "E9,F"
RUFF_IGNORE = "E501"
MYPY_CONFIG = """[mypy]
ignore_missing_imports = True
namespace_packages = True
explicit_package_bases = True
check_untyped_defs = True
show_error_codes = True
show_column_numbers = True
no_error_summary = True
pretty = False
"""
TSC_FLAGS = (
    "--noEmit",
    "--pretty",
    "false",
    "--strict",
    "--target",
    "ES2020",
    "--module",
    "ESNext",
    "--moduleResolution",
    "bundler",
    "--jsx",
    "preserve",
    "--skipLibCheck",
)
# Missing third-party modules and JSX typings only mean "nothing was installed": not findings.
TSC_NOISE = frozenset({"TS7016", "TS7026", "TS2875"})
# mypy findings that only mean "a module is not installed / not in the draft", not a code defect.
MYPY_ENVIRONMENT_CODES = frozenset({"import-not-found", "import-untyped", "import"})
_MYPY_ENVIRONMENT_MESSAGE = re.compile(
    r"^(Cannot find implementation or library stub|Skipping analyzing|Library stubs not installed"
    r'|Module "[^"]*" has no attribute|Name "[^"]*" already defined \(possible cyclic)'
)
REQUIRED_TOOLS = ("ruff", "mypy")
SYNTAX_ONLY_NOTE = "Syntax errors found: only those are reported; mypy was skipped for this step."
CATEGORY_CODE = "code"
CATEGORY_ENVIRONMENT = "environment"
_MODULE = re.compile(r"Cannot find module '([^']*)'")
_QUOTED = re.compile(r'"([^"]+)"|\'([^\']+)\'')
MAX_ENVIRONMENT_REASONS = 10
_MYPY_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)? (?P<sev>error|warning): (?P<msg>.*?)"
    r"(?:  \[(?P<code>[\w-]+)\])?$"
)
_TSC_LINE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),\d+\): (?:error|warning) (?P<code>TS\d+): (?P<msg>.*)$"
)


@dataclass(frozen=True)
class Finding:
    tool: str
    file: str
    line: int
    code: str
    message: str
    # Why a finding is environment noise; set by the classifier for names from unresolved imports.
    reason: str = ""

    @property
    def environment_reason(self) -> str:
        if self.reason:
            return self.reason
        if self.tool == "mypy" and (
            self.code in MYPY_ENVIRONMENT_CODES or _MYPY_ENVIRONMENT_MESSAGE.match(self.message)
        ):
            return "A module is not installed or not in the draft"
        return ""

    @property
    def category(self) -> str:
        """ "environment" for findings caused by what is not installed or not in the draft."""
        return CATEGORY_ENVIRONMENT if self.environment_reason else CATEGORY_CODE

    def as_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str
    timed_out: bool = False


Runner = Callable[[list[str], Path, Mapping[str, str], float], CommandResult]


@dataclass
class CheckReport:
    """`findings` holds only actionable findings (capped); everything else is counted."""

    findings: list[Finding]
    total: int  # actionable findings before the cap
    tools: dict[str, str]
    by_rule: dict[str, int] = field(default_factory=dict)  # "tool:code" -> count, all findings
    environment: int = 0  # findings caused by missing packages/modules, not by the code
    environment_reasons: dict[str, int] = field(default_factory=dict)  # reason -> count
    notes: list[str] = field(default_factory=list)

    @property
    def unavailable(self) -> list[str]:
        """Required checkers (ruff, mypy) that could not be found or started."""
        return [name for name in REQUIRED_TOOLS if self.tools.get(name) == "unavailable"]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.findings)

    def as_json(self) -> dict[str, Any]:
        return {
            "findings": [f.as_json() for f in self.findings],
            "total": self.total,
            "truncated": self.truncated,
            "tools": self.tools,
            "by_rule": dict(sorted(self.by_rule.items())),
            "not_shown": {"environment": self.environment, "reasons": self.environment_reasons},
            "notes": self.notes,
        }


def run_command(
    argv: list[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> CommandResult:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(None, "", timed_out=True)
    except OSError:
        return CommandResult(None, "")
    return CommandResult(done.returncode, done.stdout.decode("utf-8", errors="replace"))


def _parse_finding(path: str, content: str) -> Finding | None:
    """Parse only (ast, json, tomllib, yaml compose); returns a finding for a syntax error."""
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    try:
        if extension == "py":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ast.parse(content, filename="<draft>", mode="exec")
        elif extension == "json":
            json.loads(content)
        elif extension == "toml":
            tomllib.loads(content)
        elif extension in ("yaml", "yml"):
            # compose builds the node graph only: no tag is resolved to an object.
            list(yaml.compose_all(content, Loader=yaml.SafeLoader))
    except SyntaxError as exc:
        return Finding("parse", path, exc.lineno or 1, "python_syntax", (exc.msg or "")[:100])
    except json.JSONDecodeError as exc:
        return Finding("parse", path, exc.lineno, "json_invalid", exc.msg[:100])
    except tomllib.TOMLDecodeError:
        return Finding("parse", path, 1, "toml_invalid", "Invalid TOML")
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return Finding(
            "parse", path, (mark.line + 1) if mark else 1, "yaml_invalid", "Invalid YAML"
        )
    except (ValueError, RecursionError, MemoryError):
        return Finding("parse", path, 1, f"{extension}_invalid", "Could not be parsed")
    return None


def _syntax_findings(found: list[Finding]) -> list[Finding]:
    """Syntax errors only: ruff's E9/invalid-syntax, plus the parser's for files ruff missed."""
    ruff = [
        f
        for f in found
        if f.tool == "ruff" and (f.code == "invalid-syntax" or f.code.startswith("E9"))
    ]
    covered = {f.file for f in ruff}
    parsed = [f for f in found if f.code == "python_syntax" and f.file not in covered]
    return ruff + parsed


def _draft_modules(files: Mapping[str, str]) -> set[str]:
    """Dotted names of the draft's modules and their packages (what mypy can resolve)."""
    modules: set[str] = set()
    for path in files:
        if not path.endswith((".py", ".pyi")):
            continue
        parts = path.rsplit(".", 1)[0].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for end in range(1, len(parts) + 1):
            modules.add(".".join(parts[:end]))
    return modules


def _module_paths(files: Mapping[str, str]) -> dict[str, str]:
    """Dotted module name -> draft path."""
    out: dict[str, str] = {}
    for path in files:
        if path.endswith(".py"):
            parts = path[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            out[".".join(parts)] = path
    return out


def _import_bindings(
    path: str, tree: ast.AST, draft: set[str]
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Names from imports mypy cannot resolve (name -> module) and from draft modules."""
    package = path.split("/")[:-1]
    unresolved: dict[str, str] = {}
    local: list[tuple[str, str, str]] = []  # (local name, draft module, original name)

    def external(module: str) -> bool:
        return module.split(".")[0] not in sys.stdlib_module_names and module not in draft

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if external(alias.name):
                    unresolved[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if not module:
                continue
            for alias in node.names:
                if external(module):
                    unresolved[alias.asname or alias.name] = module
                elif module in draft:
                    local.append((alias.asname or alias.name, module, alias.name))
    return unresolved, local


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _taint_maps(files: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """Per draft file: names that come from an unresolved import, directly, through a
    variable or subclass, or through another draft module that re-exports such a name."""
    modules = _module_paths(files)
    draft = set(_draft_modules(files))
    trees: dict[str, ast.AST] = {}
    taint: dict[str, dict[str, str]] = {}
    locals_: dict[str, list[tuple[str, str, str]]] = {}
    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        try:
            trees[path] = ast.parse(files[path])
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
        taint[path], locals_[path] = _import_bindings(path, trees[path], draft)
    for _ in range(10):  # settles chains: Base = make(); class A(Base); from m import A
        changed = False
        for path, tree in trees.items():
            tainted = taint[path]
            for name, module, original in locals_[path]:
                origin = taint.get(modules.get(module, ""), {}).get(original)
                if origin and name not in tainted:
                    tainted[name] = origin
                    changed = True
            for node in ast.walk(tree):
                targets: list[str] = []
                sources: set[str] = set()
                if isinstance(node, ast.Assign):
                    targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    sources = _names_in(node.value)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target.id]
                    sources = _names_in(node.value) if node.value else set()
                elif isinstance(node, ast.ClassDef):
                    targets = [node.name]
                    sources = set().union(*(_names_in(b) for b in node.bases))
                origin = next((tainted[s] for s in sorted(sources) if s in tainted), None)
                if origin is not None:
                    for name in targets:
                        if name not in tainted:
                            tainted[name] = origin
                            changed = True
        if not changed:
            break
    return taint


def _classify_mypy(
    finding: Finding, files: Mapping[str, str], cache: dict[str, dict[str, str]]
) -> Finding:
    """A mypy finding that mentions a name from an unresolved import is environment noise."""
    if finding.environment_reason:
        return finding
    if not cache:
        cache.update(_taint_maps(files))
    tainted = cache.get(finding.file, {})
    if not tainted:
        return finding
    for quoted in _QUOTED.findall(finding.message):
        for part in re.split(r"[^A-Za-z0-9_]+", quoted[0] or quoted[1]):
            if part in tainted:
                reason = (
                    f'Mentions "{part}", which comes from "{tainted[part]}" '
                    "(not installed in the check environment or not in the draft)"
                )
                return replace(finding, reason=reason[:200])
    return finding


class StaticChecker:
    def __init__(
        self,
        *,
        temp_parent: str = "",
        timeout: float = 60.0,
        tsc_path: str = "",
        runner: Runner = run_command,
    ) -> None:
        self._temp_parent = temp_parent or None
        self._timeout = timeout
        self._tsc_path = tsc_path
        self._runner = runner

    # ---- public ----------------------------------------------------------------------------

    def run(self, files: Mapping[str, str]) -> CheckReport:
        taint_cache: dict[str, dict[str, str]] = {}
        found: list[Finding] = []
        notes: list[str] = []
        tools: dict[str, str] = {}
        for path in sorted(files):
            problem = _parse_finding(path, files[path])
            if problem:
                found.append(problem)
        tools["parse"] = "ok"
        python = sorted(p for p in files if p.endswith(".py"))
        typescript = sorted(p for p in files if p.endswith((".ts", ".tsx")))
        if python or typescript:
            with tempfile.TemporaryDirectory(
                prefix="archpilot-check-", dir=self._temp_parent
            ) as tmp:
                root = Path(tmp).resolve()
                work, ctl = root / "work", root / "ctl"
                work.mkdir()
                ctl.mkdir()
                (ctl / "home").mkdir()
                (ctl / "tmp").mkdir()
                written = self._write(work, files)
                env = self._env(ctl, work)
                if python:
                    py = [p for p in python if p in written]
                    found += self._ruff(work, ctl, env, py, tools)
                    syntax = _syntax_findings(found)
                    if syntax:
                        found, notes = syntax, [SYNTAX_ONLY_NOTE]
                        tools["mypy"] = "skipped_syntax_errors"
                    else:
                        found += self._mypy(work, ctl, env, py, tools)
                else:
                    tools["ruff"] = tools["mypy"] = "skipped_no_files"
                if typescript:
                    found += self._tsc(
                        work, ctl, env, [p for p in typescript if p in written], tools
                    )
                else:
                    tools["tsc"] = "skipped_no_files"
        else:
            tools["ruff"] = tools["mypy"] = tools["tsc"] = "skipped_no_files"
        found = [_classify_mypy(f, files, taint_cache) if f.tool == "mypy" else f for f in found]
        by_rule: dict[str, int] = {}
        for f in found:
            by_rule[f"{f.tool}:{f.code}"] = by_rule.get(f"{f.tool}:{f.code}", 0) + 1
        actionable = [f for f in found if f.category == CATEGORY_CODE]
        actionable.sort(key=lambda f: (f.file, f.line, f.tool, f.code))
        reasons = Counter(f.environment_reason for f in found if f.category == CATEGORY_ENVIRONMENT)
        return CheckReport(
            actionable[:MAX_FINDINGS],
            len(actionable),
            tools,
            by_rule,
            len(found) - len(actionable),
            dict(reasons.most_common(MAX_ENVIRONMENT_REASONS)),
            notes,
        )

    def status(self) -> dict[str, dict[str, Any]]:
        """Which checkers can be found and started by this interpreter, with their versions."""
        out: dict[str, dict[str, Any]] = {}
        with tempfile.TemporaryDirectory(prefix="archpilot-check-", dir=self._temp_parent) as tmp:
            ctl = Path(tmp).resolve()
            (ctl / "home").mkdir()
            (ctl / "tmp").mkdir()
            env = self._env(ctl, ctl)
            for name in REQUIRED_TOOLS:
                command = (
                    [sys.executable, "-I", "-m", name, "--version"]
                    if importlib.util.find_spec(name) is not None
                    else None
                )
                out[name] = self._probe(command, ctl, env)
            tsc = self._tsc_command()
            out["tsc"] = self._probe([*tsc, "--version"] if tsc else None, ctl, env)
        return out

    def _probe(
        self, command: list[str] | None, ctl: Path, env: Mapping[str, str]
    ) -> dict[str, Any]:
        if command is None:
            return {"available": False, "version": None}
        result = self._runner(command, ctl, env, min(self._timeout, 20.0))
        text = result.stdout.strip().splitlines()
        if result.timed_out or result.returncode != 0 or not text:
            return {"available": False, "version": None}
        return {"available": True, "version": text[0][:100]}

    # ---- plumbing --------------------------------------------------------------------------

    @staticmethod
    def _write(work: Path, files: Mapping[str, str]) -> set[str]:
        written: set[str] = set()
        for path, content in files.items():
            if path_problem(path):
                continue
            target = (work / path).resolve()
            if not target.is_relative_to(work):  # defence in depth; paths are already validated
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
            written.add(path)
        return written

    @staticmethod
    def _env(ctl: Path, work: Path) -> dict[str, str]:
        keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR", "LANG")
        env = {k: v for k in keep if (v := os.environ.get(k)) is not None}
        dead = "http://127.0.0.1:9"  # nothing listens here: any network attempt fails fast
        env.update(
            HTTP_PROXY=dead,
            HTTPS_PROXY=dead,
            ALL_PROXY=dead,
            http_proxy=dead,
            https_proxy=dead,
            all_proxy=dead,
            NO_PROXY="",
            TMP=str(ctl / "tmp"),
            TEMP=str(ctl / "tmp"),
            TMPDIR=str(ctl / "tmp"),
            HOME=str(ctl / "home"),
            USERPROFILE=str(ctl / "home"),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1",
            MYPYPATH=str(work),
            NO_COLOR="1",
        )
        return env

    def _relative(self, raw: str, base: Path, work: Path) -> str | None:
        candidate = Path(raw)
        full = candidate if candidate.is_absolute() else base / candidate
        try:
            return full.resolve().relative_to(work).as_posix()
        except (ValueError, OSError):
            return None

    @staticmethod
    def _cap(message: str) -> str:
        return message if len(message) <= MAX_MESSAGE_CHARS else message[:MAX_MESSAGE_CHARS] + "â€¦"

    def _status(
        self, name: str, result: CommandResult, tools: dict[str, str], ok_codes: set[int]
    ) -> bool:
        if result.timed_out:
            tools[name] = "timeout"
            return False
        if result.returncode is None:
            tools[name] = "unavailable"
            return False
        tools[name] = "ok" if result.returncode in ok_codes else "failed"
        return result.returncode in ok_codes

    # ---- tools -----------------------------------------------------------------------------

    def _ruff(
        self, work: Path, ctl: Path, env: Mapping[str, str], paths: list[str], tools: dict[str, str]
    ) -> list[Finding]:
        if importlib.util.find_spec("ruff") is None:
            tools["ruff"] = "unavailable"
            return []
        argv = [
            sys.executable,
            "-I",
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--output-format",
            "json",
            "--select",
            RUFF_RULES,
            "--ignore",
            RUFF_IGNORE,
            *[str(work / p) for p in paths],
        ]
        result = self._runner(argv, ctl, env, self._timeout)
        if not self._status("ruff", result, tools, {0, 1}):
            return []
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError:
            tools["ruff"] = "failed"
            return []
        out: list[Finding] = []
        for row in rows:
            file = self._relative(str(row.get("filename", "")), ctl, work)
            if file is None:
                continue
            out.append(
                Finding(
                    "ruff",
                    file,
                    int((row.get("location") or {}).get("row", 1)),
                    str(row.get("code") or "ruff"),
                    self._cap(str(row.get("message", ""))),
                )
            )
        return out

    def _mypy(
        self, work: Path, ctl: Path, env: Mapping[str, str], paths: list[str], tools: dict[str, str]
    ) -> list[Finding]:
        if importlib.util.find_spec("mypy") is None:
            tools["mypy"] = "unavailable"
            return []
        config = ctl / "mypy.ini"
        config.write_text(MYPY_CONFIG, encoding="utf-8")
        argv = [
            sys.executable,
            "-I",
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--cache-dir",
            str(ctl / "mypy_cache"),
            "--no-incremental",
            "--no-site-packages",
            *[str(work / p) for p in paths],
        ]
        result = self._runner(argv, ctl, env, self._timeout)
        if not self._status("mypy", result, tools, {0, 1}):
            return []
        out: list[Finding] = []
        for line in result.stdout.splitlines():
            match = _MYPY_LINE.match(line.strip())
            if not match:
                continue
            file = self._relative(match["file"], ctl, work)
            if file is None:
                continue
            out.append(
                Finding(
                    "mypy",
                    file,
                    int(match["line"]),
                    match["code"] or match["sev"],
                    self._cap(match["msg"]),
                )
            )
        return out

    def _tsc_command(self) -> list[str] | None:
        if self._tsc_path:
            return [self._tsc_path]
        script = REPO_ROOT / "frontend" / "node_modules" / "typescript" / "lib" / "tsc.js"
        node = shutil.which("node")
        if script.is_file() and node:
            return [node, str(script)]
        found = shutil.which("tsc")
        return [found] if found else None

    def _tsc(
        self, work: Path, ctl: Path, env: Mapping[str, str], paths: list[str], tools: dict[str, str]
    ) -> list[Finding]:
        command = self._tsc_command()
        if command is None:
            tools["tsc"] = "unavailable"
            return []
        result = self._runner(
            [*command, *TSC_FLAGS, *[str(work / p) for p in paths]], ctl, env, self._timeout
        )
        if not self._status("tsc", result, tools, {0, 1, 2}):
            return []
        out: list[Finding] = []
        for line in result.stdout.splitlines():
            match = _TSC_LINE.match(line.strip())
            if not match or match["code"] in TSC_NOISE:
                continue
            module = _MODULE.search(match["msg"])
            if match["code"] == "TS2307" and module and not module[1].startswith("."):
                continue
            file = self._relative(match["file"], ctl, work)
            if file is None:
                continue
            out.append(
                Finding("tsc", file, int(match["line"]), match["code"], self._cap(match["msg"]))
            )
        return out
