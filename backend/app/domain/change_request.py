"""Free-form change requests: impact classification, architecture diff and drift detection.

Pure and deterministic. Classification is advisory: a person can always override the class.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.architecture import _DECISION_GROUPS, ArchitectureContent

STATUSES = ("draft", "analysed", "approved", "in_progress", "done", "rejected")
CLASSES = ("code_only", "architecture")
KEY_PREFIX = "CR-"
MAX_TEXT = 4000

_STOP = frozenset(
    [
        "this",
        "that",
        "with",
        "from",
        "into",
        "have",
        "will",
        "should",
        "would",
        "could",
        "must",
        "also",
        "them",
        "they",
        "then",
        "than",
        "when",
        "where",
        "which",
        "what",
        "your",
        "more",
        "some",
        "such",
        "only",
        "each",
        "other",
        "about",
        "after",
        "before",
        "over",
        "under",
        "make",
        "need",
        "needs",
        "want",
        "add",
        "new",
        "update",
        "change",
        "changes",
        "support",
        "allow",
    ]
)

_NEW = r"(?:new|add(?:ing)?|introduc\w*|creat\w*|build|extract\w*|split|separate|dedicated)"
_SIGNALS: dict[str, tuple[re.Pattern[str], str]] = {
    "new_component": (
        re.compile(
            rf"\b{_NEW}\b[^.\n]{{0,40}}\b(?:micro-?services?|services?|components?|workers?|"
            r"gateways?|queues?|brokers?|schedulers?|daemons?|caches?|proxy|proxies)\b",
            re.I,
        ),
        "introduces a new component",
    ),
    "integration": (
        re.compile(
            r"\b(?:integrat\w*|webhooks?|third[- ]party|external (?:api|system|service)s?|"
            r"sync(?:hroni[sz]e)? with|connect(?:s|ing)? to)\b|https?://",
            re.I,
        ),
        "adds or changes an integration",
    ),
    "technology": (
        re.compile(
            r"\b(?:migrat\w*|switch\w*|replac\w*|rewrit\w*|port)\b[^.\n]{0,30}\b(?:to|from|with|in)\b",
            re.I,
        ),
        "changes a technology",
    ),
    "data_store": (
        re.compile(
            r"\b(?:databases?|data ?stores?|data warehouse|redis|mongo\w*|postgres\w*|mysql|"
            r"elasticsearch|blob storage|s3 bucket|object storage)\b",
            re.I,
        ),
        "touches a data store",
    ),
    "security": (
        re.compile(
            r"\b(?:authenticat\w*|authori[sz]\w*|oauth\w*|sso|saml|encrypt\w*|mfa|2fa|rbac|"
            r"tls|secrets?|gdpr|pii|compliance|permission model|audit log)\b",
            re.I,
        ),
        "changes security",
    ),
    "nfr": (
        re.compile(
            r"\b(?:scalab\w*|latency|throughput|availability|uptime|sla|high availability|"
            r"failover|disaster recovery|rpo|rto|\d+\s?ms|\d+\s?(?:concurrent )?users)\b",
            re.I,
        ),
        "changes a non-functional requirement",
    ),
}
# Mentioning a data store, a security term or a number is only architecture when the request also
# asks for something new; these are the terms that need a verb of change next to them.
_NEEDS_CHANGE_VERB = frozenset({"data_store", "security", "nfr"})
_CHANGE_VERB = re.compile(
    rf"\b(?:{_NEW}|migrat\w*|switch\w*|replac\w*|move|introduc\w*|require\w*|enforce\w*|"
    r"enable|implement\w*|support|store|persist\w*|improve|reduce|increase|guarantee)\b",
    re.I,
)
_TECH_TERMS = frozenset(
    [
        "postgres",
        "postgresql",
        "mysql",
        "mongodb",
        "redis",
        "kafka",
        "rabbitmq",
        "kubernetes",
        "docker",
        "graphql",
        "grpc",
        "react",
        "angular",
        "vue",
        "django",
        "flask",
        "fastapi",
        "spring",
        "golang",
        "rust",
        "typescript",
        "terraform",
        "aws",
        "azure",
        "gcp",
        "elasticsearch",
        "s3",
        "oauth",
        "saml",
        "nginx",
        "celery",
    ]
)
_SECTION_LABEL = {
    "recommended_stack": "stack",
    "integrations": "integration",
    "data_architecture": "data",
    "security_architecture": "security",
    "non_functional_design": "non_functional",
}


def tokens(text: str) -> set[str]:
    out = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) >= 4 and word not in _STOP:
            out.add(word[:-1] if word.endswith("s") and len(word) > 4 else word)
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def architecture_corpus(content: ArchitectureContent) -> str:
    """Everything the architecture names, lowercased and normalised, for 'is it covered?'."""
    parts: list[str] = [content.solution_overview]
    for c in content.components:
        parts += [c.id, c.name, c.responsibility, c.technology or ""]
    for group in _DECISION_GROUPS:
        for d in getattr(content, group):
            parts += [d.name, d.decision, getattr(d, "technology", "") or ""]
    return _norm(" ".join(parts))


@dataclass
class Impact:
    impact_class: str
    reason: str
    signals: list[dict[str, str]] = field(default_factory=list)
    components: list[dict[str, str]] = field(default_factory=list)
    decisions: list[dict[str, str]] = field(default_factory=list)
    requirements: list[dict[str, str]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "impact_class": self.impact_class,
            "reason": self.reason,
            "signals": self.signals,
            "components": self.components,
            "decisions": self.decisions,
            "requirements": self.requirements,
            "files": self.files,
        }


def classify(
    text: str,
    content: ArchitectureContent,
    decisions: Iterable[Mapping[str, str]],
    requirements: Iterable[Mapping[str, str]],
    files: Sequence[str] = (),
) -> Impact:
    """Classify a request as `code_only` or `architecture` from the approved architecture, the
    binding human decisions, the confirmed requirements and the repository files."""
    corpus = architecture_corpus(content)
    words = tokens(text)
    signals: list[dict[str, str]] = []
    has_verb = bool(_CHANGE_VERB.search(text))
    for kind, (pattern, label) in _SIGNALS.items():
        match = pattern.search(text)
        if match is None or (kind in _NEEDS_CHANGE_VERB and not has_verb):
            continue
        signals.append({"kind": kind, "label": label, "evidence": match.group(0)[:80]})
    new_tech = sorted(t for t in _TECH_TERMS if t in words and t not in corpus.split())
    if new_tech and not any(s["kind"] == "technology" for s in signals):
        signals.append(
            {
                "kind": "technology",
                "label": "names a technology the architecture does not use",
                "evidence": ", ".join(new_tech),
            }
        )
    # A data-store/security/NFR term that the architecture already covers and that is not
    # being changed is not a signal on its own: it needs a change verb (checked above).

    affected_components = [
        {"id": c.id, "name": c.name}
        for c in content.components
        if (tokens(c.name) | tokens(c.id)) & words
        or (c.technology and _norm(c.technology) in _norm(text))
    ]
    affected_decisions: list[dict[str, str]] = []
    for group in _DECISION_GROUPS:
        for d in getattr(content, group):
            if (tokens(d.name) | tokens(getattr(d, "technology", "") or "")) & words:
                affected_decisions.append(
                    {"source": "architecture", "ref": f"{group}:{d.name}", "name": d.name}
                )
    for d in decisions:
        choice = _norm(d["choice"])
        if choice and re.search(rf"\b{re.escape(choice)}\b", _norm(text)):
            affected_decisions.append(
                {"source": "binding", "ref": d["area"], "name": f"{d['area']}: {d['choice']}"}
            )
            if re.search(
                r"\b(?:migrat\w*|switch\w*|replac\w*|drop|instead of|move away)\b", text, re.I
            ):
                signals.append(
                    {
                        "kind": "binding_decision",
                        "label": "contradicts a binding decision",
                        "evidence": d["choice"],
                    }
                )
    affected_requirements = []
    for r in requirements:
        own = tokens(r["title"])
        shared = own & words
        if shared and (len(shared) >= 2 or len(shared) / max(len(own), 1) >= 0.6):
            affected_requirements.append({"id": r["id"], "title": r["title"]})

    if signals:
        kinds = ", ".join(sorted({s["label"] for s in signals}))
        return Impact(
            "architecture",
            f"Architecture impact: the request {kinds}.",
            signals,
            affected_components,
            affected_decisions,
            affected_requirements,
            list(files),
        )
    where = (
        "existing component(s) " + ", ".join(c["name"] for c in affected_components)
        if affected_components
        else "existing code"
    )
    return Impact(
        "code_only",
        f"Code only: no new component, integration, technology, data store, security or "
        f"non-functional change; the request fits {where}.",
        [],
        affected_components,
        affected_decisions,
        affected_requirements,
        list(files),
    )


# ---- architecture diff -------------------------------------------------------------------------


def _decision_view(d: Any) -> dict[str, Any]:
    return {
        "name": d.name,
        "decision": d.decision,
        "technology": getattr(d, "technology", None),
        "origin": d.origin.value,
    }


def _component_view(c: Any) -> dict[str, Any]:
    return {
        "name": c.name,
        "type": c.type.value,
        "responsibility": c.responsibility,
        "technology": c.technology,
    }


def _compare(
    parent: Mapping[str, dict[str, Any]], child: Mapping[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    added = [{"key": k, **v} for k, v in child.items() if k not in parent]
    removed = [{"key": k, **v} for k, v in parent.items() if k not in child]
    changed = []
    for k, v in child.items():
        if k in parent and parent[k] != v:
            fields = sorted(f for f in v if v[f] != parent[k].get(f))
            changed.append({"key": k, "name": v["name"], "fields": fields, "before": parent[k]})
    return {"added": added, "removed": removed, "changed": changed}


def diff_architecture(parent: ArchitectureContent, child: ArchitectureContent) -> dict[str, Any]:
    """Component and decision diff of `child` against its parent version."""
    components = _compare(
        {c.id: _component_view(c) for c in parent.components},
        {c.id: _component_view(c) for c in child.components},
    )
    decisions: dict[str, list[dict[str, Any]]] = {"added": [], "removed": [], "changed": []}
    for group in _DECISION_GROUPS:
        found = _compare(
            {_norm(d.name): _decision_view(d) for d in getattr(parent, group)},
            {_norm(d.name): _decision_view(d) for d in getattr(child, group)},
        )
        for kind, items in found.items():
            decisions[kind] += [{**i, "section": _SECTION_LABEL[group]} for i in items]
    return {
        "components": components,
        "decisions": decisions,
        "removals": [
            *(
                {"kind": "component", "key": c["key"], "name": c["name"]}
                for c in components["removed"]
            ),
            *(
                {"kind": "decision", "key": d["key"], "name": d["name"]}
                for d in decisions["removed"]
            ),
        ],
        "unchanged": {
            "components": len(child.components)
            - len(components["added"])
            - len(components["changed"]),
        },
    }


def removal_ids(diff: Mapping[str, Any]) -> set[str]:
    return {f"{r['kind']}:{r['key']}" for r in diff.get("removals", [])}


# ---- drift ---------------------------------------------------------------------------------------

_SOURCE_ROOTS = frozenset(
    {"src", "app", "lib", "backend", "frontend", "packages", "services", "apps"}
)
_CODE_EXT = frozenset({"py", "ts", "tsx", "js", "jsx", "java", "go", "cs", "rb", "rs", "kt"})
_TEST_PART = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|(\.|_)(test|spec)\.|^test_|/test_")
_URL = re.compile(r"https?://([A-Za-z0-9.-]+)")
_IGNORED_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "w3.org", "schema.org", "json-schema.org")


def structure(paths: Iterable[str]) -> dict[str, list[str]]:
    """The top-level packages (directories) and modules (files) of the source files."""
    packages: set[str] = set()
    modules: set[str] = set()
    for path in paths:
        name = path.replace("\\", "/").lstrip("./")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _CODE_EXT or _TEST_PART.search(name):
            continue
        parts = name.split("/")
        skipped = 0
        while len(parts) > 1 and parts[0].lower() in _SOURCE_ROOTS and skipped < 2:
            parts = parts[1:]
            skipped += 1
        if len(parts) >= 2:
            packages.add(parts[0])
        else:
            stem = parts[0].rsplit(".", 1)[0]
            if stem not in ("__init__", "index", "main", "app"):
                modules.add(stem)
    return {"packages": sorted(packages), "modules": sorted(modules)}


_GENERIC = frozenset({"utils", "util", "common", "shared", "helpers", "core", "config", "types"})


def _covered(name: str, corpus: str) -> bool:
    n = _norm(name)
    return n in _GENERIC or bool(n) and (n in corpus or any(t in corpus for t in tokens(name)))


def detect_drift(
    content: ArchitectureContent,
    baseline: Mapping[str, Sequence[str]] | None,
    paths: Iterable[str],
    new_texts: Mapping[str, str],
) -> list[dict[str, str]]:
    """New packages, new top-level modules and new external endpoints that the approved
    architecture does not name. Packages and modules are only compared against a baseline
    snapshot taken before the change; endpoints are read from the files the patch wrote."""
    corpus = architecture_corpus(content)
    items: list[dict[str, str]] = []
    if baseline is not None:
        now = structure(paths)
        for kind, key in (("package", "packages"), ("module", "modules")):
            for name in now[key]:
                if name not in baseline.get(key, ()) and not _covered(name, corpus):
                    items.append(
                        {
                            "kind": f"new_{kind}",
                            "name": name,
                            "detail": f"New top-level {kind} '{name}' is not in the architecture.",
                        }
                    )
    seen: set[str] = set()
    for path, text in sorted(new_texts.items()):
        for host in _URL.findall(text):
            host = host.lower().rstrip(".")
            if host in seen or any(host == h or host.endswith("." + h) for h in _IGNORED_HOSTS):
                continue
            seen.add(host)
            labels = host.split(".")
            if host in corpus or any(
                len(label) >= 4 and label in corpus.split() for label in labels
            ):
                continue
            items.append(
                {
                    "kind": "new_endpoint",
                    "name": host,
                    "detail": f"{path} calls external endpoint {host}, which the architecture "
                    "does not name.",
                }
            )
    return items
