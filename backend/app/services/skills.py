"""Loading skills from disk and the per-project skill catalog (Phase 14B).

Sources: `backend/skills/` (built-in; `backend/skills/vendor/<publisher>/` holds vendored skills
next to their LICENSE) and `<project clone>/.archpilot/skills/`. A project skill with the name of
a built-in one overrides it and is labelled. Symlinks, junctions and path traversal are refused;
nothing in a skill folder is ever executed.
"""

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.domain.skills import (
    BUILTIN,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PROJECT_SKILLS,
    PROJECT,
    SKILL_FILE,
    VENDORED,
    Skill,
    SkillError,
    SkillProblem,
    build_skill,
    relative_path_problem,
)

logger = logging.getLogger(__name__)

BUILTIN_ROOT = Path(__file__).resolve().parents[2] / "skills"
PROJECT_SKILLS_PARTS = (".archpilot", "skills")
LICENSE_FILE = "LICENSE"
MAX_DEPTH = 4


class SkillNotFoundError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown skill: {name[:60]}")


def _is_link(entry: "os.DirEntry[str] | Path") -> bool:
    if entry.is_symlink():
        return True
    junction = getattr(entry, "is_junction", None)
    return bool(junction()) if callable(junction) else False


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    return resolved == root or root in resolved.parents


def read_skill_dir(directory: Path) -> dict[str, bytes]:
    """Every file of one skill folder as {relative path: bytes}; refuses links and oversize."""
    if _is_link(directory):
        raise SkillError("symlink", "The skill folder is a symlink")
    root = directory.resolve()
    blobs: dict[str, bytes] = {}

    def walk(current: Path, prefix: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise SkillError("too_deep", "The skill folder is nested too deeply")
        with os.scandir(current) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            if _is_link(entry):
                raise SkillError("symlink", f"Symlinks are not allowed: {rel[:80]}")
            if relative_path_problem(rel):
                raise SkillError("path_invalid", f"Unacceptable file name: {rel[:80]}")
            path = Path(entry.path)
            if not _inside(path, root):
                raise SkillError("path_traversal", f"Outside the skill folder: {rel[:80]}")
            if entry.is_dir(follow_symlinks=False):
                walk(path, f"{rel}/", depth + 1)
            elif entry.is_file(follow_symlinks=False):
                if len(blobs) >= MAX_FILES:
                    raise SkillError("too_many_files", f"A skill has at most {MAX_FILES} files")
                if entry.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES:
                    raise SkillError("file_too_large", f"{rel[:80]} is too large")
                blobs[rel] = path.read_bytes()
            else:
                raise SkillError("not_regular", f"Not a regular file: {rel[:80]}")

    walk(directory, "", 0)
    return blobs


def _children(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.iterdir() if not p.name.startswith((".", "_")))
    except OSError:
        return []


def _load(
    directory: Path, source: str, found: dict[str, Skill], problems: list[SkillProblem],
    *, license_required: bool = False,
) -> None:  # fmt: skip
    name = directory.name
    try:
        blobs = read_skill_dir(directory)
        skill = build_skill(name, blobs, source, license_required=license_required)
    except SkillError as exc:
        problems.append(SkillProblem(name[:60], source, exc.code, str(exc)))
        return
    except OSError:
        problems.append(SkillProblem(name[:60], source, "unreadable", "The folder cannot be read"))
        return
    if name in found:
        problems.append(SkillProblem(name[:60], source, "duplicate", "Another skill has this name"))
        return
    found[name] = skill


def scan_builtin(root: Path = BUILTIN_ROOT) -> tuple[dict[str, Skill], list[SkillProblem]]:
    found: dict[str, Skill] = {}
    problems: list[SkillProblem] = []
    if not root.is_dir():
        return found, problems
    for child in _children(root):
        if child.name == "vendor":
            continue
        if _is_link(child):
            problems.append(SkillProblem(child.name[:60], BUILTIN, "symlink", "A symlink"))
        elif child.is_dir():
            _load(child, BUILTIN, found, problems)
    vendor = root / "vendor"
    if vendor.is_dir() and not _is_link(vendor):
        for publisher in _children(vendor):
            if _is_link(publisher) or not publisher.is_dir():
                continue
            licensed = (publisher / LICENSE_FILE).is_file() and not _is_link(
                publisher / LICENSE_FILE
            )
            for child in _children(publisher):
                if not child.is_dir() and not _is_link(child):
                    continue
                if not licensed:
                    problems.append(
                        SkillProblem(
                            child.name[:60], VENDORED, "license_missing",
                            f"{publisher.name} has no {LICENSE_FILE} file",
                        )
                    )  # fmt: skip
                    continue
                _load(child, VENDORED, found, problems, license_required=True)
    return found, problems


def scan_project(clone: Path) -> tuple[dict[str, Skill], list[SkillProblem]]:
    found: dict[str, Skill] = {}
    problems: list[SkillProblem] = []
    root = clone.resolve()
    directory = root
    for part in PROJECT_SKILLS_PARTS:
        directory = directory / part
        if _is_link(directory):
            problems.append(SkillProblem(part, PROJECT, "symlink", f"{part} is a symlink"))
            return found, problems
    if not directory.is_dir() or not _inside(directory, root):
        return found, problems
    for index, child in enumerate(_children(directory)):
        if index >= MAX_PROJECT_SKILLS:
            problems.append(
                SkillProblem("", PROJECT, "too_many_skills", f"At most {MAX_PROJECT_SKILLS} skills")
            )
            break
        if _is_link(child):
            problems.append(SkillProblem(child.name[:60], PROJECT, "symlink", "A symlink"))
        elif child.is_dir():
            _load(child, PROJECT, found, problems)
    return found, problems


@dataclass
class SkillCatalog:
    """The skills one project sees: built-in and vendored, with project skills on top."""

    skills: dict[str, Skill] = field(default_factory=dict)
    problems: list[SkillProblem] = field(default_factory=list)
    disabled: frozenset[str] = frozenset()

    def enabled(self) -> list[Skill]:
        return [s for _, s in sorted(self.skills.items()) if s.name not in self.disabled]

    def get(self, name: str) -> Skill | None:
        """An enabled skill by name."""
        skill = self.skills.get(name)
        return skill if skill is not None and name not in self.disabled else None


class SkillStore(Protocol):
    """Where the per-project on/off switches live."""

    def disabled_skills(self, project_id: uuid.UUID) -> set[str]: ...

    def set_skill_enabled(self, project_id: uuid.UUID, name: str, enabled: bool) -> None: ...


class SkillService:
    def __init__(
        self,
        store: SkillStore | None = None,
        project_root: Callable[[uuid.UUID], Path | None] = lambda _pid: None,
        builtin_root: Path = BUILTIN_ROOT,
    ) -> None:
        self._store = store
        self._project_root = project_root
        self._builtin_root = builtin_root

    def catalog(self, project_id: uuid.UUID) -> SkillCatalog:
        skills, problems = scan_builtin(self._builtin_root)
        clone = self._project_root(project_id)
        if clone is not None:
            mine, more = scan_project(clone)
            problems += more
            for name, skill in mine.items():
                base = skills.get(name)
                skills[name] = skill.with_override(base.source) if base is not None else skill
        disabled = self._store.disabled_skills(project_id) if self._store else set()
        return SkillCatalog(skills, problems, frozenset(disabled))

    def states(self, project_id: uuid.UUID) -> dict[str, Any]:
        catalog = self.catalog(project_id)
        return {
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "source": s.source,
                    "overrides": s.overrides,
                    "license": s.license,
                    "version": s.version,
                    "content_hash": s.content_hash,
                    "enabled": s.name not in catalog.disabled,
                    "file_count": len(s.files),
                    "ignored_keys": list(s.ignored_keys),
                }
                for _, s in sorted(catalog.skills.items())
            ],
            "problems": [
                {"name": p.name, "source": p.source, "code": p.code, "message": p.message}
                for p in catalog.problems
            ],
        }

    def detail(self, project_id: uuid.UUID, name: str) -> dict[str, Any]:
        catalog = self.catalog(project_id)
        skill = catalog.skills.get(name)
        if skill is None:
            raise SkillNotFoundError(name)
        state = next(s for s in self.states(project_id)["skills"] if s["name"] == name)
        return {
            **state,
            "body": skill.body,
            "files": [{"path": f.path, "size": f.size, "text": f.text} for f in skill.files],
        }

    def set_enabled(self, project_id: uuid.UUID, name: str, enabled: bool) -> None:
        if name not in self.catalog(project_id).skills:
            raise SkillNotFoundError(name)
        if self._store is not None:
            self._store.set_skill_enabled(project_id, name, enabled)


__all__ = [
    "BUILTIN_ROOT",
    "SKILL_FILE",
    "SkillCatalog",
    "SkillNotFoundError",
    "SkillService",
    "SkillStore",
    "read_skill_dir",
    "scan_builtin",
    "scan_project",
]
