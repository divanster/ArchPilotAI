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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.domain.agent import MAX_FINDINGS
from app.domain.patch import path_problem
from app.services.workspace_export import REPO_ROOT

MAX_MESSAGE_CHARS = 300
RUFF_RULES = "E,F,B"
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
_MODULE = re.compile(r"Cannot find module '([^']*)'")
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
    findings: list[Finding]
    total: int
    tools: dict[str, str]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.findings)

    def as_json(self) -> dict[str, Any]:
        return {
            "findings": [f.as_json() for f in self.findings],
            "total": self.total,
            "truncated": self.truncated,
            "tools": self.tools,
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
        found: list[Finding] = []
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
                    found += self._ruff(work, ctl, env, [p for p in python if p in written], tools)
                    found += self._mypy(work, ctl, env, [p for p in python if p in written], tools)
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
        found.sort(key=lambda f: (f.file, f.line, f.tool, f.code))
        return CheckReport(found[:MAX_FINDINGS], len(found), tools)

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
        return message if len(message) <= MAX_MESSAGE_CHARS else message[:MAX_MESSAGE_CHARS] + "…"

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
