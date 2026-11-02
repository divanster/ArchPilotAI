"""Agent skills: validation of one skill folder, held in memory (Phase 14B).

A skill is a folder with SKILL.md (frontmatter `name` and `description`, then the body) and
optional references/, scripts/ and assets/. Skill text is untrusted instruction text: it is only
ever shown to the model as a tool result. It cannot change tools, tiers, hooks, approvals,
budgets or the command allowlist, and no skill file is ever executed (scripts are reference
text). Frontmatter keys other than name, description, version and license are ignored.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

SKILL_FILE = "SKILL.md"
SUBDIRS = ("references", "scripts", "assets")

# "skill:" + name must fit the 40-character per-project setting key.
MAX_NAME_CHARS = 34
MAX_DESCRIPTION_CHARS = 1024
MAX_VERSION_CHARS = 40
MAX_LICENSE_CHARS = 60
MAX_SKILL_MD_BYTES = 16_000  # a loaded skill body always fits one tool result
MAX_FILE_BYTES = 64_000
MAX_TOTAL_BYTES = 512_000
MAX_FILES = 40
MAX_PATH_CHARS = 200
MAX_PROJECT_SKILLS = 20

BUILTIN = "built-in"
VENDORED = "vendored"
PROJECT = "project"
SOURCES = (BUILTIN, VENDORED, PROJECT)

KNOWN_KEYS = frozenset({"name", "description", "version", "license"})
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\s*:(?:\s+(.*))?$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_SPACE = re.compile(r"\s+")


class SkillError(Exception):
    """A skill (or file of one) that is refused; `code` is fixed, the message is short."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SkillFile:
    path: str
    size: int
    text: bool


@dataclass(frozen=True)
class SkillProblem:
    """A skill folder that was not loaded, and why."""

    name: str
    source: str
    code: str
    message: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    source: str  # built-in | vendored | project
    version: str
    license: str
    content_hash: str
    files: tuple[SkillFile, ...]
    ignored_keys: tuple[str, ...] = ()
    overrides: str | None = None  # the source of the built-in skill a project skill replaces
    blobs: Mapping[str, bytes] = field(default_factory=dict, repr=False, compare=False)

    def reference(self) -> dict[str, str]:
        """What a run records about a skill it used."""
        return {
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "content_hash": self.content_hash,
        }

    def with_override(self, overrides: str) -> "Skill":
        return Skill(
            self.name, self.description, self.body, self.source, self.version, self.license,
            self.content_hash, self.files, self.ignored_keys, overrides, self.blobs,
        )  # fmt: skip


def relative_path_problem(raw: str) -> str | None:
    """The failure code for an unacceptable skill-relative path, or None."""
    if not raw or len(raw) > MAX_PATH_CHARS:
        return "path_invalid"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return "path_invalid"
    if raw.startswith(("/", "\\")):
        return "path_absolute"
    if _DRIVE.match(raw):
        return "path_drive"
    parts = raw.replace("\\", "/").split("/")
    if ".." in parts:
        return "path_traversal"
    if "\\" in raw or ":" in raw or any(p in ("", ".") for p in parts):
        return "path_invalid"
    return None


def _scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            value = json.loads(text)
        except ValueError:
            raise SkillError("frontmatter_syntax", "Invalid quoted value in frontmatter") from None
        return str(value)
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Flat `key: value` frontmatter between two `---` lines, and the body after it.

    No YAML features (anchors, nesting, multi-line values) are supported on purpose."""
    lines = text.replace("\r\n", "\n").lstrip("\ufeff").split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillError("frontmatter_missing", "SKILL.md must start with --- frontmatter")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise SkillError("frontmatter_missing", "The frontmatter is not closed with ---")
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _KEY.match(line)
        if match is None:
            raise SkillError("frontmatter_syntax", "Frontmatter supports only `key: value` lines")
        key = match.group(1)
        if key in meta:
            raise SkillError("frontmatter_duplicate", f"Duplicate frontmatter key: {key[:40]}")
        meta[key] = _scalar(match.group(2) or "")
    return meta, "\n".join(lines[end + 1 :]).strip("\n")


def is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def content_hash(blobs: Mapping[str, bytes]) -> str:
    """SHA-256 over every file (sorted by path); text files are line-ending normalised so the
    hash is the same on every checkout."""
    digest = hashlib.sha256()
    for path in sorted(blobs):
        data = blobs[path]
        if is_text(data):
            data = data.replace(b"\r\n", b"\n")
        digest.update(path.encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


def check_files(blobs: Mapping[str, bytes]) -> None:
    if len(blobs) > MAX_FILES:
        raise SkillError("too_many_files", f"A skill has at most {MAX_FILES} files")
    if sum(len(b) for b in blobs.values()) > MAX_TOTAL_BYTES:
        raise SkillError("skill_too_large", f"A skill is at most {MAX_TOTAL_BYTES} bytes in total")
    for path, data in blobs.items():
        problem = relative_path_problem(path)
        if problem:
            raise SkillError(problem, f"Unacceptable file path: {path[:80]}")
        first = path.split("/", 1)[0]
        if path != SKILL_FILE and (first not in SUBDIRS or "/" not in path):
            raise SkillError(
                "path_layout", f"Only {SKILL_FILE}, references/, scripts/ and assets/: {path[:80]}"
            )
        limit = MAX_SKILL_MD_BYTES if path == SKILL_FILE else MAX_FILE_BYTES
        if len(data) > limit:
            raise SkillError("file_too_large", f"{path[:80]} is larger than {limit} bytes")


def build_skill(
    folder: str, blobs: Mapping[str, bytes], source: str, *, license_required: bool = False
) -> Skill:
    """Validates the files of one skill folder and returns the skill, or raises SkillError."""
    check_files(blobs)
    raw = blobs.get(SKILL_FILE)
    if raw is None:
        raise SkillError("skill_md_missing", f"No {SKILL_FILE}")
    if not is_text(raw):
        raise SkillError("not_text", f"{SKILL_FILE} is not UTF-8 text")
    meta, body = parse_frontmatter(raw.decode("utf-8"))
    name = meta.get("name", "")
    if not name or len(name) > MAX_NAME_CHARS or not _NAME.fullmatch(name):
        raise SkillError(
            "name_invalid",
            f"name must be lowercase letters, digits and hyphens, at most {MAX_NAME_CHARS}",
        )
    if name != folder:
        raise SkillError("name_mismatch", "The name must equal the folder name")
    description = _SPACE.sub(" ", meta.get("description", "")).strip()
    if not description:
        raise SkillError("description_missing", "description is required")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillError(
            "description_too_long", f"description is at most {MAX_DESCRIPTION_CHARS} characters"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in description):
        raise SkillError("description_invalid", "description has control characters")
    if not body.strip():
        raise SkillError("body_empty", "The skill has no body")
    version = meta.get("version", "").strip()
    license_name = meta.get("license", "").strip()
    if len(version) > MAX_VERSION_CHARS or len(license_name) > MAX_LICENSE_CHARS:
        raise SkillError("metadata_too_long", "version or license is too long")
    if license_required and not license_name:
        raise SkillError("license_missing", "A vendored skill names its license")
    files = tuple(SkillFile(path, len(blobs[path]), is_text(blobs[path])) for path in sorted(blobs))
    return Skill(
        name=name,
        description=description,
        body=body,
        source=source,
        version=version,
        license=license_name,
        content_hash=content_hash(blobs),
        files=files,
        ignored_keys=tuple(sorted(set(meta) - KNOWN_KEYS)),
        blobs=dict(blobs),
    )
