"""Repository intelligence (Phase 13C): pure rules for mapping a connected repository.

Nothing here touches the disk, the network or a process. Python is parsed with `ast`, TypeScript
and JavaScript with regular expressions, manifests with `json` / `tomllib`; repository code is
never executed or imported. Instruction files (AGENTS.md, README.md, ...) are project context,
never authority over ArchPilotAI's rules or permissions.
"""

import ast
import json
import posixpath
import re
import tomllib
import warnings
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.patch import has_secret

MAP_VERSION = 1
MAX_FILE_BYTES = 256 * 1024  # larger files are excluded ("too_large")
MAX_FILES = 20_000  # files visited per scan; the map says when it stopped early
MAX_DEPTH = 30
MAX_TOTAL_BYTES = 64 * 1024 * 1024  # text read per scan
MAX_SYMBOLS_PER_FILE = 300
MAX_IMPORTS_PER_FILE = 200
MAX_LISTED = 50  # entries per summary list
MAX_INSTRUCTION_FILES = 12
MAX_INSTRUCTION_CHARS = 4_000

# Instruction files, in the order they are offered.
INSTRUCTION_NAMES: dict[str, int] = {
    "agents.md": 0,
    "claude.md": 1,
    "project.md": 2,
    "readme.md": 3,
}

DEPENDENCY_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "virtualenv",
        ".tox",
        ".nox",
        "site-packages",
        "bower_components",
        "jspm_packages",
        ".pnpm-store",
        ".yarn",
    }
)
BUILD_DIRS = frozenset(
    {
        "build",
        "dist",
        "out",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        ".cache",
        ".output",
        ".vercel",
        ".angular",
        "target",
        "coverage",
        "htmlcov",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        ".eggs",
        "storybook-static",
    }
)
VCS_DIRS = frozenset({".git", ".hg", ".svn"})
TOOLING_DIRS = frozenset({".idea", ".vscode", ".archpilot"})

_SECRET_NAME = re.compile(
    r"""^(
        \.env(\..*)?
      | .*\.(pem|key|p8|p12|pfx|crt|cer|der|csr|jks|keystore|kdbx|ppk|asc|gpg|pgp|tfvars|tfstate)
      | .*\.tfstate\.backup
      | id_(rsa|dsa|ecdsa|ed25519)(\.pub)?
      | \.netrc | \.npmrc | \.pypirc | \.htpasswd | \.git-credentials | \.dockercfg
      | credentials(\..*)?
      | secrets?(\..*)?
      | service[-_]?account.*\.json
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

BINARY_EXTENSIONS = frozenset(
    [
        "png",
        "jpg",
        "jpeg",
        "gif",
        "bmp",
        "ico",
        "icns",
        "webp",
        "tif",
        "tiff",
        "psd",
        "ai",
        "pdf",
        "zip",
        "gz",
        "tgz",
        "bz2",
        "xz",
        "7z",
        "rar",
        "tar",
        "jar",
        "war",
        "ear",
        "class",
        "so",
        "dll",
        "dylib",
        "exe",
        "bin",
        "o",
        "a",
        "lib",
        "obj",
        "pyc",
        "pyo",
        "pyd",
        "whl",
        "egg",
        "woff",
        "woff2",
        "ttf",
        "otf",
        "eot",
        "mp3",
        "mp4",
        "m4a",
        "mov",
        "avi",
        "mkv",
        "wav",
        "ogg",
        "webm",
        "flac",
        "sqlite",
        "sqlite3",
        "db",
        "mdb",
        "parquet",
        "pkl",
        "pickle",
        "npy",
        "npz",
        "h5",
        "onnx",
        "pt",
        "ckpt",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "odt",
        "ods",
        "dmg",
        "iso",
        "apk",
        "ipa",
        "wasm",
    ]
)

LANGUAGES: dict[str, str] = {
    "py": "Python",
    "pyi": "Python",
    "ts": "TypeScript",
    "tsx": "TypeScript",
    "mts": "TypeScript",
    "cts": "TypeScript",
    "js": "JavaScript",
    "jsx": "JavaScript",
    "mjs": "JavaScript",
    "cjs": "JavaScript",
    "vue": "Vue",
    "svelte": "Svelte",
    "html": "HTML",
    "htm": "HTML",
    "css": "CSS",
    "scss": "SCSS",
    "sass": "SCSS",
    "less": "Less",
    "md": "Markdown",
    "rst": "reStructuredText",
    "json": "JSON",
    "yaml": "YAML",
    "yml": "YAML",
    "toml": "TOML",
    "ini": "INI",
    "cfg": "INI",
    "sql": "SQL",
    "sh": "Shell",
    "bash": "Shell",
    "ps1": "PowerShell",
    "go": "Go",
    "rs": "Rust",
    "java": "Java",
    "kt": "Kotlin",
    "rb": "Ruby",
    "php": "PHP",
    "cs": "C#",
    "c": "C",
    "h": "C",
    "cpp": "C++",
    "hpp": "C++",
    "swift": "Swift",
    "txt": "Text",
}
PYTHON_EXT = frozenset({"py", "pyi"})
SCRIPT_EXT = frozenset({"ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs"})
_RESOLVE_EXT = ("ts", "tsx", "d.ts", "js", "jsx", "mjs", "cjs", "mts", "cts", "json", "vue")

TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "specs", "e2e", "testing"})
_TEST_FILE = re.compile(
    r"^(test_.*\.py|.*_test\.py|conftest\.py|.*\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs|mts))$"
)

_LOCK_FILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "pipfile.lock",
        "uv.lock",
        "pdm.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
    }
)
_GENERATED_NAME = re.compile(
    r"^(.*\.min\.(js|css)|.*\.map|.*_pb2(_grpc)?\.pyi?|.*\.pb\.go|.*\.generated\.\w+|.*\.gen\.(ts|js))$",
    re.IGNORECASE,
)
_MIGRATION = re.compile(r"(^|/)migrations/\d{4}_[^/]*\.py$")
_GENERATED_MARK = re.compile(
    r"@generated|do not edit|auto-?generated|generated by django|code generated by",
    re.IGNORECASE,
)

_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "pipfile",
        "setup.py",
        "setup.cfg",
        "go.mod",
        "cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "gemfile",
        "composer.json",
        "makefile",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "tsconfig.json",
        "tox.ini",
        "pytest.ini",
        "mypy.ini",
        "ruff.toml",
        ".ruff.toml",
        ".flake8",
    }
)
_REQUIREMENTS = re.compile(r"^requirements([-_.][^/]*)?\.txt$", re.IGNORECASE)

PY_FRAMEWORKS: dict[str, str] = {
    "django": "Django",
    "djangorestframework": "Django REST framework",
    "channels": "Django Channels",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "starlette": "Starlette",
    "celery": "Celery",
    "sqlalchemy": "SQLAlchemy",
    "pydantic": "Pydantic",
    "pytest": "pytest",
    "aiohttp": "aiohttp",
    "tornado": "Tornado",
    "pyramid": "Pyramid",
    "streamlit": "Streamlit",
}
JS_FRAMEWORKS: dict[str, str] = {
    "react": "React",
    "react-native": "React Native",
    "next": "Next.js",
    "vue": "Vue",
    "nuxt": "Nuxt",
    "@angular/core": "Angular",
    "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit",
    "@remix-run/react": "Remix",
    "astro": "Astro",
    "express": "Express",
    "@nestjs/core": "NestJS",
    "fastify": "Fastify",
    "electron": "Electron",
    "vite": "Vite",
    "webpack": "webpack",
    "vitest": "Vitest",
    "jest": "Jest",
    "@playwright/test": "Playwright",
    "cypress": "Cypress",
    "typescript": "TypeScript",
    "eslint": "ESLint",
    "tailwindcss": "Tailwind CSS",
}
FILE_FRAMEWORKS: dict[str, str] = {
    "go.mod": "Go modules",
    "cargo.toml": "Cargo",
    "pom.xml": "Maven",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    "gemfile": "Bundler",
    "composer.json": "Composer",
    "dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "docker-compose.yaml": "Docker Compose",
    "compose.yml": "Docker Compose",
    "compose.yaml": "Docker Compose",
}


# ---- path rules ------------------------------------------------------------------------------


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def extension(path: str) -> str:
    name = _name(path)
    return name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""


def language_of(path: str) -> str | None:
    if _name(path).lower() == "dockerfile":
        return "Dockerfile"
    if _name(path).lower() == "makefile":
        return "Makefile"
    return LANGUAGES.get(extension(path))


def dir_exclusion(name: str) -> str | None:
    """Why a directory is not scanned (None: scan it)."""
    lowered = name.lower()
    if lowered in VCS_DIRS:
        return "vcs"
    if lowered in DEPENDENCY_DIRS:
        return "dependencies"
    if lowered in BUILD_DIRS or lowered.endswith(".egg-info"):
        return "build_output"
    if lowered in TOOLING_DIRS:
        return "tooling"
    return None


def path_exclusion(path: str) -> str | None:
    """Why a file is excluded by its name alone (None: read it)."""
    if _SECRET_NAME.match(_name(path)):
        return "secret_name"
    if extension(path) in BINARY_EXTENSIONS:
        return "binary"
    return None


def decode_text(path: str, data: bytes) -> tuple[str | None, str | None]:
    """(text, None) for a readable text file, else (None, reason). Secrets are never kept."""
    if b"\x00" in data[:8192]:
        return None, "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "binary"
    if has_secret(path, text):
        return None, "secret_content"
    return text, None


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    return bool(_TEST_FILE.match(parts[-1])) or any(p.lower() in TEST_DIRS for p in parts[:-1])


def is_generated(path: str, text: str) -> bool:
    name = _name(path)
    if name.lower() in _LOCK_FILES or _GENERATED_NAME.match(name) or _MIGRATION.search(path):
        return True
    head = "\n".join(text[:2000].splitlines()[:5])
    return bool(_GENERATED_MARK.search(head))


def is_manifest(path: str) -> bool:
    name = _name(path)
    lowered = name.lower()
    return (
        lowered in _MANIFEST_NAMES
        or bool(_REQUIREMENTS.match(name))
        or lowered.endswith(".csproj")
        or lowered == "manage.py"
    )


def instruction_rank(path: str) -> int | None:
    return INSTRUCTION_NAMES.get(_name(path).lower())


# ---- per-file facts --------------------------------------------------------------------------


Symbol = tuple[str, str, int]  # (name, kind, line)


@dataclass
class FileFacts:
    path: str
    language: str | None
    size: int
    lines: int
    generated: bool
    test: bool
    symbols: list[Symbol] = field(default_factory=list)
    # Python: (level, module, names); scripts: ("", specifier, []).
    raw_imports: list[tuple[int, str, list[str]]] = field(default_factory=list)
    has_main: bool = False


def _python_facts(facts: FileFacts, text: str) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text, filename="<scan>", mode="exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return
    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.append((node.name, "function", node.lineno))
        elif isinstance(node, ast.ClassDef):
            symbols.append((node.name, "class", node.lineno))
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    symbols.append((f"{node.name}.{item.name}", "method", item.lineno))
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols.append((target.id, "variable", node.lineno))
        elif isinstance(node, ast.If) and _is_main_guard(node.test):
            facts.has_main = True
    imports: list[tuple[int, str, list[str]]] = []
    for found in ast.walk(tree):
        if isinstance(found, ast.Import):
            imports += [(0, alias.name, []) for alias in found.names]
        elif isinstance(found, ast.ImportFrom):
            imports.append((found.level, found.module or "", [a.name for a in found.names]))
    facts.symbols = symbols[:MAX_SYMBOLS_PER_FILE]
    facts.raw_imports = imports[:MAX_IMPORTS_PER_FILE]


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


_IDENT = r"[A-Za-z_$][\w$]*"
_TS_SYMBOLS: list[tuple[str, re.Pattern[str]]] = [
    (
        "function",
        re.compile(
            rf"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s*({_IDENT})",
            re.M,
        ),
    ),
    (
        "class",
        re.compile(
            rf"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?class\s+({_IDENT})",
            re.M,
        ),
    ),
    ("interface", re.compile(rf"^(?:export\s+)?(?:declare\s+)?interface\s+({_IDENT})", re.M)),
    (
        "type",
        re.compile(rf"^(?:export\s+)?(?:declare\s+)?type\s+({_IDENT})\s*(?:<[^=\n]*>)?\s*=", re.M),
    ),
    ("enum", re.compile(rf"^(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+({_IDENT})", re.M)),
    (
        "variable",
        re.compile(rf"^(?:export\s+)?(?:declare\s+)?(?:const|let|var)\s+({_IDENT})", re.M),
    ),
]
_TS_IMPORTS = [
    re.compile(
        r"""\b(?:import|export)\s+(?:type\s+)?(?:[\w*{}\s,$]+?\s+from\s+)?["']([^"'\n]+)["']"""
    ),
    re.compile(r"""\brequire\(\s*["']([^"'\n]+)["']\s*\)"""),
    re.compile(r"""\bimport\(\s*["']([^"'\n]+)["']\s*\)"""),
]


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _script_facts(facts: FileFacts, text: str) -> None:
    found: list[Symbol] = []
    for kind, pattern in _TS_SYMBOLS:
        found += [(m.group(1), kind, _line_of(text, m.start())) for m in pattern.finditer(text)]
    found.sort(key=lambda s: (s[2], s[0]))
    facts.symbols = found[:MAX_SYMBOLS_PER_FILE]
    specs: list[str] = []
    for pattern in _TS_IMPORTS:
        specs += [m.group(1) for m in pattern.finditer(text)]
    no_names: list[str] = []
    facts.raw_imports = [(0, s, no_names) for s in dict.fromkeys(specs)][:MAX_IMPORTS_PER_FILE]


def analyse_file(path: str, text: str, size: int) -> FileFacts:
    """Facts about one readable text file. Parsing only: nothing is executed or imported."""
    generated = is_generated(path, text)
    facts = FileFacts(
        path=path,
        language=language_of(path),
        size=size,
        lines=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        generated=generated,
        test=is_test_path(path),
    )
    if generated:
        return facts
    ext = extension(path)
    if ext in PYTHON_EXT:
        _python_facts(facts, text)
    elif ext in SCRIPT_EXT:
        _script_facts(facts, text)
    return facts


# ---- import resolution -----------------------------------------------------------------------


def _dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


class _PythonIndex:
    def __init__(self, paths: Iterable[str]) -> None:
        py = sorted(p for p in paths if extension(p) == "py")
        self.init_dirs = {_dir(p) for p in py if _name(p) == "__init__.py" and "/" in p}
        self.modules: dict[str, list[tuple[str, str]]] = {}
        self.location: dict[str, tuple[str, list[str], bool]] = {}
        for path in py:
            root, parts, is_init = self._split(path)
            self.location[path] = (root, parts, is_init)
            if parts:
                self.modules.setdefault(".".join(parts), []).append((root, path))
            full = [*path.split("/")[:-1]]
            if not is_init:
                full.append(_name(path)[:-3])
            if full and full != parts and all(p.isidentifier() for p in full):
                self.modules.setdefault(".".join(full), []).append(("", path))

    def _split(self, path: str) -> tuple[str, list[str], bool]:
        dirs = path.split("/")[:-1]
        stem = _name(path)[:-3]
        i = len(dirs)
        while i > 0 and "/".join(dirs[:i]) in self.init_dirs:
            i -= 1
        is_init = stem == "__init__"
        return "/".join(dirs[:i]), dirs[i:] + ([] if is_init else [stem]), is_init

    def find(self, module: str, root: str, same_root_only: bool = False) -> str | None:
        options = self.modules.get(module, [])
        same = [p for r, p in options if r == root]
        if same:
            return same[0]
        if same_root_only or not options:
            return None
        return sorted(p for _, p in options)[0]

    def resolve(self, path: str, level: int, module: str, names: list[str]) -> list[str]:
        root, parts, is_init = self.location[path]
        if level == 0:
            if names:
                return self._from(module.split("."), names, root, False)
            pieces = module.split(".")
            for end in range(len(pieces), 0, -1):
                found = self.find(".".join(pieces[:end]), root)
                if found:
                    return [found]
            return []
        package = parts if is_init else parts[:-1]
        if level - 1 > len(package):
            return []
        base = package[: len(package) - (level - 1)]
        target = base + ([p for p in module.split(".") if p] if module else [])
        return self._from(target, names, root, True)

    def _from(self, target: list[str], names: list[str], root: str, local: bool) -> list[str]:
        found: list[str] = []
        for name in names:
            if name != "*":
                sub = self.find(".".join([*target, name]), root, local)
                if sub:
                    found.append(sub)
        if not found and target:
            hit = self.find(".".join(target), root, local)
            if hit:
                found.append(hit)
        return found


def _resolve_script(path: str, spec: str, known: set[str]) -> str | None:
    if not spec.startswith(("./", "../")) and spec not in {".", ".."}:
        return None  # packages and aliases are outside the map
    joined = posixpath.normpath(posixpath.join(_dir(path), spec.split("?")[0]))
    if joined.startswith("..") or joined.startswith("/"):
        return None
    candidates = [joined]
    stem, dot, ext = joined.rpartition(".")
    if dot and ext in {"js", "jsx", "mjs", "cjs"}:
        candidates += [f"{stem}.ts", f"{stem}.tsx", f"{stem}.mts", f"{stem}.cts"]
    candidates += [f"{joined}.{e}" for e in _RESOLVE_EXT]
    candidates += [f"{joined}/index.{e}" for e in _RESOLVE_EXT]
    return next((c for c in candidates if c in known), None)


def resolve_imports(facts: Mapping[str, FileFacts]) -> dict[str, list[str]]:
    """File-to-file edges inside the repository (external packages are left out)."""
    known = set(facts)
    index = _PythonIndex(known)
    edges: dict[str, list[str]] = {}
    for path in sorted(facts):
        item = facts[path]
        targets: list[str] = []
        ext = extension(path)
        for level, module, names in item.raw_imports:
            if ext == "py":
                targets += index.resolve(path, level, module, names)
            elif ext in SCRIPT_EXT:
                hit = _resolve_script(path, module, known)
                if hit:
                    targets.append(hit)
        edges[path] = sorted({t for t in targets if t != path})
    return edges


def reverse_edges(edges: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    reverse: dict[str, set[str]] = {}
    for source, targets in edges.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)
    return {k: sorted(v) for k, v in sorted(reverse.items())}


# ---- manifests -------------------------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _req_names(lines: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for line in lines:
        if isinstance(line, str):
            match = _REQ_LINE.match(line)
            if match:
                out.add(_norm(match.group(1)))
    return out


def _toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError):
        return {}


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _table(data: Any, *keys: str) -> dict[str, Any]:
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


def python_dependencies(path: str, text: str) -> set[str]:
    name = _name(path).lower()
    if name.endswith(".txt"):
        return _req_names(
            line for line in text.splitlines() if not line.lstrip().startswith(("#", "-"))
        )
    if name == "pyproject.toml":
        data = _toml(text)
        project = _table(data, "project")
        deps = _req_names(project.get("dependencies") or [])
        for group in _table(project, "optional-dependencies").values():
            deps |= _req_names(group if isinstance(group, list) else [])
        for group in _table(data, "dependency-groups").values():
            deps |= _req_names(group if isinstance(group, list) else [])
        poetry = _table(data, "tool", "poetry")
        deps |= {_norm(k) for k in _table(poetry, "dependencies")}
        deps |= {_norm(k) for k in _table(poetry, "dev-dependencies")}
        for group in _table(poetry, "group").values():
            deps |= {_norm(k) for k in _table(group, "dependencies")}
        return deps - {"python"}
    if name == "pipfile":
        data = _toml(text)
        return {_norm(k) for k in [*_table(data, "packages"), *_table(data, "dev-packages")]}
    return set()


def package_json(text: str) -> dict[str, Any]:
    data = _json(text)
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps |= set(_table(data, key))
    scripts = {k: v for k, v in _table(data, "scripts").items() if isinstance(v, str)}
    bins = data.get("bin")
    bin_paths = (
        [bins]
        if isinstance(bins, str)
        else [v for v in bins.values() if isinstance(v, str)]
        if isinstance(bins, dict)
        else []
    )
    mains = [data[k] for k in ("main", "module") if isinstance(data.get(k), str)]
    return {"deps": deps, "scripts": scripts, "bins": bin_paths, "mains": mains}


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.M)


def make_targets(text: str) -> list[str]:
    return sorted({m.group(1) for m in _MAKE_TARGET.finditer(text)})


_LINT_SCRIPTS = frozenset(
    {"lint", "eslint", "stylelint", "typecheck", "type-check", "tsc", "check", "format:check"}
)


def script_category(name: str) -> str | None:
    lowered = name.lower()
    if lowered.endswith((":fix", ":watch")) or lowered.startswith(("pre", "post")):
        return None
    if lowered in _LINT_SCRIPTS or lowered.startswith("lint:"):
        return "lint"
    if lowered == "test" or lowered.startswith("test:"):
        return "test"
    if lowered == "build" or lowered.startswith("build:"):
        return "build"
    return None


# ---- the map ---------------------------------------------------------------------------------


@dataclass
class ScanResult:
    facts: dict[str, FileFacts]
    texts: dict[str, str]  # manifests and manage.py, for summary analysis only
    instructions: dict[str, str]
    excluded_files: Counter[str]
    excluded_dirs: Counter[str]
    truncated: bool = False


def _cmd(command: str, cwd: str, source: str) -> dict[str, str]:
    return {"command": command, "cwd": cwd or ".", "source": source}


def _analyse_manifests(
    scan: ScanResult, py_index: _PythonIndex
) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    dict[str, list[dict[str, str]]],
    list[dict[str, str]],
]:
    """(frameworks, scripts, commands, extra entry points) from manifest text only."""
    paths = set(scan.facts)
    frameworks: dict[str, str] = {}
    scripts: list[dict[str, str]] = []
    commands: dict[str, list[dict[str, str]]] = {"lint": [], "test": [], "build": []}
    entries: list[dict[str, str]] = []
    py_deps: dict[str, set[str]] = {}
    py_dirs: set[str] = set()
    pyproject: dict[str, dict[str, Any]] = {}

    def found(name: str, evidence: str) -> None:
        if name not in frameworks or evidence < frameworks[name]:
            frameworks[name] = evidence

    for path in sorted(scan.texts):
        text = scan.texts[path]
        name = _name(path)
        lowered = name.lower()
        where = _dir(path)
        if lowered in FILE_FRAMEWORKS:
            found(FILE_FRAMEWORKS[lowered], path)
        if lowered.endswith(".csproj"):
            found(".NET", path)
        if lowered == "package.json":
            info = package_json(text)
            for dep in sorted(info["deps"]):
                if dep in JS_FRAMEWORKS:
                    found(JS_FRAMEWORKS[dep], path)
            runner = (
                "yarn"
                if posixpath.join(where, "yarn.lock").lstrip("/") in paths
                else "pnpm"
                if posixpath.join(where, "pnpm-lock.yaml").lstrip("/") in paths
                else "npm"
            )
            for script, command in sorted(info["scripts"].items()):
                scripts.append({"source": path, "name": script, "command": command[:200]})
                category = script_category(script)
                if category:
                    run = (
                        "npm test"
                        if runner == "npm" and script == "test"
                        else f"npm run {script}"
                        if runner == "npm"
                        else f"{runner} {script}"
                    )
                    commands[category].append(_cmd(run, where, path))
            for kind, targets in (("package_main", info["mains"]), ("package_bin", info["bins"])):
                for target in targets:
                    resolved = posixpath.normpath(posixpath.join(where, target))
                    if resolved in paths:
                        entries.append({"path": resolved, "kind": kind})
        elif lowered == "makefile":
            for target in make_targets(text)[:40]:
                scripts.append({"source": path, "name": target, "command": f"make {target}"})
                category = {"lint": "lint", "check": "lint", "typecheck": "lint"}.get(
                    target, script_category(target)
                )
                if category:
                    commands[category].append(_cmd(f"make {target}", where, path))
        elif lowered == "manage.py":
            py_dirs.add(where)
            if "django" in text.lower():
                found("Django", path)
        if lowered == "pyproject.toml" or lowered == "pipfile" or _REQUIREMENTS.match(name):
            py_dirs.add(where)
            deps = python_dependencies(path, text)
            py_deps.setdefault(where, set()).update(deps)
            for dep in sorted(deps):
                if dep in PY_FRAMEWORKS:
                    found(PY_FRAMEWORKS[dep], path)
        if lowered == "pyproject.toml":
            data = _toml(text)
            pyproject[where] = data
            for script, target in sorted(_table(data, "project", "scripts").items()):
                if isinstance(target, str):
                    scripts.append({"source": path, "name": script, "command": target[:200]})
                    module = target.split(":", 1)[0].strip()
                    hit = py_index.find(module, where)
                    if hit:
                        entries.append({"path": hit, "kind": "console_script"})
        if lowered in {"setup.cfg", "tox.ini", "pytest.ini", "mypy.ini", ".flake8"}:
            py_dirs.add(where)

    for where in sorted(py_dirs):

        def has(name: str, where: str = where) -> bool:
            return posixpath.join(where, name).lstrip("/") in paths

        deps = py_deps.get(where, set())
        data = pyproject.get(where, {})
        tool = _table(data, "tool")
        setup_cfg = scan.texts.get(posixpath.join(where, "setup.cfg").lstrip("/"), "")
        tox_ini = scan.texts.get(posixpath.join(where, "tox.ini").lstrip("/"), "")
        source = next(
            (
                posixpath.join(where, n).lstrip("/")
                for n in ("pyproject.toml", "setup.cfg", "requirements.txt", "manage.py", "tox.ini")
                if has(n)
            ),
            where or ".",
        )
        if "ruff" in tool or has("ruff.toml") or has(".ruff.toml") or "ruff" in deps:
            commands["lint"].append(_cmd("ruff check .", where, source))
        if "flake8" in deps or has(".flake8") or "[flake8]" in setup_cfg or "[flake8]" in tox_ini:
            commands["lint"].append(_cmd("flake8", where, source))
        if "black" in tool or "black" in deps:
            commands["lint"].append(_cmd("black --check .", where, source))
        if "mypy" in tool or has("mypy.ini") or "mypy" in deps or "[mypy]" in setup_cfg:
            commands["lint"].append(_cmd("mypy .", where, source))
        pytest_cfg = (
            "pytest" in tool
            or has("pytest.ini")
            or has("conftest.py")
            or has("tests/conftest.py")
            or "pytest" in deps
            or "[tool:pytest]" in setup_cfg
        )
        if pytest_cfg:
            commands["test"].append(_cmd("pytest", where, source))
        elif has("manage.py"):
            commands["test"].append(_cmd("python manage.py test", where, source))
        if has("tox.ini"):
            commands["test"].append(_cmd("tox", where, source))
        if "build-system" in data:
            commands["build"].append(_cmd("python -m build", where, source))

    for category, items in commands.items():
        unique = {(c["cwd"], c["command"]): c for c in items}
        commands[category] = [unique[k] for k in sorted(unique)][:MAX_LISTED]
    return (
        [{"name": n, "evidence": e} for n, e in sorted(frameworks.items())],
        scripts[:100],
        commands,
        entries,
    )


_JS_ENTRY = re.compile(r"(^|/)src/(main|index)\.(ts|tsx|js|jsx|mjs)$")
_PY_ENTRY = {
    "manage.py": "django_manage",
    "wsgi.py": "wsgi",
    "asgi.py": "asgi",
    "__main__.py": "python_main",
}


def _entry_points(
    scan: ScanResult, extra: list[dict[str, str]], package_dirs: list[str]
) -> list[dict[str, str]]:
    found: dict[str, str] = {}
    for item in extra:
        found.setdefault(item["path"], item["kind"])
    for path, facts in sorted(scan.facts.items()):
        if facts.generated:
            continue
        name = _name(path)
        if name in _PY_ENTRY:
            found.setdefault(path, _PY_ENTRY[name])
        elif facts.has_main and not facts.test:
            found.setdefault(path, "python_script")
        elif _JS_ENTRY.search(path):
            found.setdefault(path, "frontend_entry")
    for where in package_dirs:
        html = posixpath.join(where, "index.html").lstrip("/")
        if html in scan.facts:
            found.setdefault(html, "html_entry")
    return [{"path": p, "kind": k} for p, k in sorted(found.items())][:MAX_LISTED]


def _minimal(dirs: Iterable[str]) -> list[str]:
    """Directories without an ancestor in the set ("." stands for the repository root)."""
    ordered = sorted(set(dirs), key=lambda d: (d.count("/"), d))
    kept: list[str] = []
    for d in ordered:
        if "." in kept or any(d == k or d.startswith(k + "/") for k in kept):
            continue
        kept.append(d)
    return sorted(kept)


def build_map(scan: ScanResult) -> dict[str, Any]:
    """The deterministic map of a scanned repository: summary, files with symbols and imports,
    and bounded instruction files."""
    facts = scan.facts
    edges = resolve_imports(facts)
    py_index = _PythonIndex(facts)
    frameworks, scripts, commands, extra_entries = _analyse_manifests(scan, py_index)
    package_dirs = sorted(_dir(p) for p in scan.texts if _name(p).lower() == "package.json")

    languages: dict[str, list[int]] = {}
    for item in facts.values():
        if item.language and not item.generated:
            counts = languages.setdefault(item.language, [0, 0])
            counts[0] += 1
            counts[1] += item.size

    source_roots: Counter[str] = Counter()
    test_dirs: set[str] = set()
    for path, item in facts.items():
        if item.generated:
            continue
        if item.test:
            parts = path.split("/")[:-1]
            top = next((i for i, p in enumerate(parts) if p.lower() in TEST_DIRS), None)
            test_dirs.add(
                "/".join(parts[: top + 1]) if top is not None else ("/".join(parts) or ".")
            )
        elif extension(path) == "py":
            root = py_index.location[path][0]
            source_roots[root or "."] += 1
    for where in package_dirs:
        src = posixpath.join(where, "src").lstrip("/")
        if any(p.startswith(src + "/") for p in facts):
            source_roots[src] += 1
    test_roots = _minimal(test_dirs)
    roots = sorted(r for r in source_roots if r not in test_roots)

    generated = sorted(p for p, f in facts.items() if f.generated)
    manifests = sorted(p for p in scan.texts if is_manifest(p) and _name(p).lower() != "manage.py")

    ranked = sorted(
        scan.instructions,
        key=lambda p: (p.count("/"), instruction_rank(p) or 0, p),
    )[:MAX_INSTRUCTION_FILES]
    instructions = [
        {
            "path": p,
            "chars": len(scan.instructions[p]),
            "truncated": len(scan.instructions[p]) > MAX_INSTRUCTION_CHARS,
            "content": scan.instructions[p][:MAX_INSTRUCTION_CHARS],
        }
        for p in ranked
    ]

    files: dict[str, dict[str, Any]] = {}
    for path in sorted(facts):
        item = facts[path]
        kind = (
            "generated"
            if item.generated
            else "test"
            if item.test
            else "source"
            if extension(path) in PYTHON_EXT | SCRIPT_EXT
            else "other"
        )
        files[path] = {
            "lang": item.language,
            "bytes": item.size,
            "lines": item.lines,
            "kind": kind,
            "symbols": [list(s) for s in item.symbols],
            "imports": edges.get(path, []),
        }

    excluded_total = sum(scan.excluded_files.values())
    summary = {
        "files_total": len(facts),
        "truncated": scan.truncated,
        "languages": [
            {"language": name, "files": c[0], "bytes": c[1]}
            for name, c in sorted(languages.items(), key=lambda kv: (-kv[1][0], kv[0]))
        ],
        "frameworks": frameworks,
        "manifests": manifests[:MAX_LISTED],
        "entry_points": _entry_points(scan, extra_entries, package_dirs),
        "source_roots": roots[:MAX_LISTED],
        "test_roots": test_roots[:MAX_LISTED],
        "generated": {"count": len(generated), "sample": generated[:20]},
        "excluded": {
            "files": excluded_total,
            "directories": sum(scan.excluded_dirs.values()),
            "by_reason": dict(sorted((scan.excluded_files + scan.excluded_dirs).items())),
        },
        "scripts": scripts,
        "commands": commands,
        "edges": sum(len(v) for v in edges.values()),
    }
    return {
        "version": MAP_VERSION,
        "summary": summary,
        "instructions": instructions,
        "files": files,
    }
