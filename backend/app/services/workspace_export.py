"""Explicit export of the accepted workspace to a local folder OUTSIDE the ArchPilotAI repository.

Only files from the accepted set are written, nothing is ever deleted, and a folder that holds
files ArchPilotAI did not produce is refused unless the person confirms the overwrite. Ownership is
tracked by a manifest (path -> SHA-256) written next to the exported files. Nothing is executed.
"""

import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.patch import path_problem
from app.models.patch import WorkspaceFile

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_NAME = ".archpilot-manifest.json"
MAX_SCAN_ENTRIES = 20_000
MAX_REPORTED = 50


class WorkspaceNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__("ARCHPILOT_WORKSPACE_ROOT is not configured; set it to a local folder")


class WorkspaceLocationError(Exception):
    pass


class WorkspaceEmptyError(Exception):
    def __init__(self) -> None:
        super().__init__("The workspace has no accepted files to export")


class ForeignFilesError(Exception):
    def __init__(self, paths: Sequence[str], total: int) -> None:
        self.paths = list(paths)
        self.total = total
        super().__init__(
            f"The export folder holds {total} file(s) not produced by ArchPilotAI; "
            "confirm the overwrite to export anyway"
        )


class ExportError(Exception):
    pass


@dataclass(frozen=True)
class ExportResult:
    folder: str
    written: list[str]
    unchanged: list[str]
    overwritten: list[str]
    # Exported earlier but no longer accepted: left on disk, never deleted.
    left_on_disk: list[str] = field(default_factory=list)
    foreign_untouched: int = 0


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_folder(root_setting: str, project_id: uuid.UUID) -> Path:
    """The export folder of one project; refuses a missing, relative or in-repository root."""
    if not root_setting.strip():
        raise WorkspaceNotConfiguredError
    root = Path(root_setting.strip()).expanduser()
    if not root.is_absolute():
        raise WorkspaceLocationError("ARCHPILOT_WORKSPACE_ROOT must be an absolute path")
    resolved = root.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise WorkspaceLocationError(
            "ARCHPILOT_WORKSPACE_ROOT must be outside the ArchPilotAI repository"
        )
    return resolved / str(project_id)


def _load_manifest(folder: Path, project_id: uuid.UUID) -> dict[str, str]:
    path = folder / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data["files"]
        if data["project_id"] != str(project_id) or not isinstance(files, dict):
            return {}
        return {str(k): str(v) for k, v in files.items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _scan(folder: Path) -> tuple[list[str], set[str]]:
    """Relative POSIX paths of every entry in the folder, and which of them are symlinks."""
    found: list[str] = []
    links: set[str] = set()
    for current, dirs, names in os.walk(folder, followlinks=False):
        base = Path(current)
        for name in dirs:
            if (base / name).is_symlink():
                rel = (base / name).relative_to(folder).as_posix()
                links.add(rel)
                found.append(rel)
        for name in names:
            entry = base / name
            rel = entry.relative_to(folder).as_posix()
            if entry.is_symlink():
                links.add(rel)
            found.append(rel)
        if len(found) > MAX_SCAN_ENTRIES:
            raise ExportError("The export folder holds too many files to check safely")
        # A symlinked directory is reported above and never entered.
        dirs[:] = [d for d in dirs if not (base / d).is_symlink()]
    return found, links


def _ensure_parent(folder: Path, relative: str) -> Path:
    current = folder
    parts = relative.split("/")
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ExportError("A symbolic link blocks the export path")
        if current.exists() and not current.is_dir():
            raise ExportError("A file blocks the export path of a directory")
        current.mkdir(exist_ok=True)
    target = current / parts[-1]
    if target.is_symlink() or target.is_dir():
        raise ExportError("A symbolic link or directory blocks the export path")
    if folder.resolve() not in target.resolve().parents:
        raise ExportError("The export path leaves the workspace folder")
    return target


def _write(target: Path, data: bytes) -> None:
    temp = target.with_name(target.name + ".archpilot-tmp")
    temp.write_bytes(data)
    os.replace(temp, target)


def export_workspace(
    root_setting: str,
    project_id: uuid.UUID,
    files: Sequence[WorkspaceFile],
    *,
    confirm_overwrite: bool,
) -> ExportResult:
    if not files:
        raise WorkspaceEmptyError
    folder = project_folder(root_setting, project_id)
    for f in files:
        if path_problem(f.path):
            raise ExportError("The workspace holds a path that cannot be exported")
    if folder.is_symlink():
        raise ExportError("The export folder is a symbolic link")
    folder.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(folder, project_id)
    on_disk, links = _scan(folder)
    accepted = {f.path: f for f in files}

    foreign: list[str] = []
    for rel in sorted(on_disk):
        if rel == MANIFEST_NAME:
            continue
        if rel in links:
            if rel in accepted or any(p.startswith(rel + "/") for p in accepted):
                raise ExportError("A symbolic link blocks an export path")
            foreign.append(rel)
        elif rel not in manifest:
            foreign.append(rel)
        else:
            try:
                owned = _sha((folder / rel).read_bytes()) == manifest[rel]
            except OSError:
                owned = False
            if not owned:
                foreign.append(rel)
    if foreign and not confirm_overwrite:
        raise ForeignFilesError(foreign[:MAX_REPORTED], len(foreign))

    written: list[str] = []
    unchanged: list[str] = []
    overwritten: list[str] = []
    new_manifest = dict(manifest)
    foreign_set = set(foreign)
    for path in sorted(accepted):
        data = accepted[path].content.encode("utf-8")
        target = _ensure_parent(folder, path)
        if target.exists() and _sha(target.read_bytes()) == _sha(data):
            unchanged.append(path)
        else:
            if path in foreign_set:
                overwritten.append(path)
            _write(target, data)
            written.append(path)
        new_manifest[path] = _sha(data)
    _write(
        folder / MANIFEST_NAME,
        json.dumps({"project_id": str(project_id), "files": new_manifest}, indent=1).encode(
            "utf-8"
        ),
    )
    left = sorted(p for p in manifest if p not in accepted and (folder / p).exists())
    return ExportResult(
        folder=str(folder),
        written=written,
        unchanged=unchanged,
        overwritten=overwritten,
        left_on_disk=left[:MAX_REPORTED],
        foreign_untouched=len(foreign_set - set(overwritten)),
    )
