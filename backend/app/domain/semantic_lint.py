"""Post-generation semantic lint for architecture proposals.

Deterministic and advisory: no model calls, and the proposal is never changed, rejected or
repaired. Every rule only reads the proposal and returns findings for a person to weigh.
Findings carry fixed rule codes, schema paths and terms from the configured lists below; they
never quote free text from model output.

Known limits: term matching is lexical (a product named in other words, or one missing from the
list, is not seen), and the wording rule cannot tell a claim from a quotation or a negation
beyond the simple "not" / "never" prefix check.
"""

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.domain.architecture import (
    ArchitectureContent,
    CoverageStatus,
    DecisionOrigin,
    NodeType,
    StackItem,
    cited_passage_ids,
)

LINT_VERSION = 1


class LintRule(StrEnum):
    ORIGIN_OVERCLAIM = "origin_overclaim"
    PREFERENCE_OVERCLAIM = "preference_overclaim"
    COMPLETED_IMPLEMENTATION_WORDING = "completed_implementation_wording"
    COVERAGE_ALL_ADDRESSED = "coverage_all_addressed"
    FRONTEND_ONLY_BACKEND_REQUIREMENT = "frontend_only_backend_requirement"
    PASSAGES_NOT_CITED = "passages_not_cited"


@dataclass(frozen=True)
class ProductTerm:
    """A named product or service. `aliases` are other spellings of the same thing."""

    name: str
    aliases: tuple[str, ...] = ()
    # Ambiguous English words (Express, Spring) only count when capitalised as written.
    case_sensitive: bool = False


DEFAULT_PRODUCT_TERMS: tuple[ProductTerm, ...] = (
    ProductTerm("Azure Key Vault", ("Key Vault", "KeyVault")),
    ProductTerm("Azure Blob Storage", ("Blob Storage", "Azure Blob")),
    ProductTerm("Azure Active Directory", ("Entra ID", "Azure AD", "Microsoft Entra")),
    ProductTerm("Azure Service Bus", ("Service Bus",)),
    ProductTerm("Azure Functions"),
    ProductTerm("Azure App Service", ("App Service",)),
    ProductTerm("Azure Kubernetes Service", ("AKS",)),
    ProductTerm("Azure Cosmos DB", ("Cosmos DB", "CosmosDB")),
    ProductTerm("Azure SQL", ("Azure SQL Database",)),
    ProductTerm("Azure AI Search", ("Azure Cognitive Search",)),
    ProductTerm("Application Insights", ("App Insights",)),
    ProductTerm("Amazon S3", ("AWS S3", "S3 bucket")),
    ProductTerm("AWS Lambda", ("Lambda",), case_sensitive=True),
    ProductTerm("AWS Cognito", ("Amazon Cognito", "Cognito")),
    ProductTerm("Amazon SQS", ("AWS SQS",)),
    ProductTerm("Google Cloud Storage", ("GCS",), case_sensitive=True),
    ProductTerm("Kubernetes", ("K8s",)),
    ProductTerm("Docker"),
    ProductTerm("Terraform"),
    ProductTerm("PostgreSQL", ("Postgres",)),
    ProductTerm("MySQL"),
    ProductTerm("MongoDB", ("Mongo DB",)),
    ProductTerm("SQL Server", ("MSSQL",)),
    ProductTerm("Oracle Database", ("Oracle DB",)),
    ProductTerm("Redis"),
    ProductTerm("Elasticsearch", ("Elastic Search",)),
    ProductTerm("OpenSearch"),
    ProductTerm("RabbitMQ"),
    ProductTerm("Apache Kafka", ("Kafka",)),
    ProductTerm("Keycloak"),
    ProductTerm("Auth0"),
    ProductTerm("Okta"),
    ProductTerm("Node.js", ("NodeJS", "Node js")),
    ProductTerm("Express", ("Express.js", "ExpressJS"), case_sensitive=True),
    ProductTerm("NestJS", ("Nest.js",)),
    ProductTerm("FastAPI"),
    ProductTerm("Django"),
    ProductTerm("Flask", case_sensitive=True),
    ProductTerm("Spring Boot", ("Spring Framework",)),
    ProductTerm(".NET", ("ASP.NET", "dotnet")),
    ProductTerm("React", ("ReactJS", "React.js"), case_sensitive=True),
    ProductTerm("Angular", case_sensitive=True),
    ProductTerm("Vue.js", ("VueJS",)),
    ProductTerm("Next.js", ("NextJS",)),
    ProductTerm("GraphQL"),
    ProductTerm("gRPC"),
    ProductTerm("OAuth 2.0", ("OAuth2", "OAuth")),
    ProductTerm("OpenID Connect", ("OIDC",), case_sensitive=True),
    ProductTerm("SAML", case_sensitive=True),
    ProductTerm("Power BI", ("PowerBI",)),
    ProductTerm("SharePoint"),
    ProductTerm("Salesforce"),
    ProductTerm("SAP", case_sensitive=True),
)

DEFAULT_WORDING_PHRASES: tuple[str, ...] = (
    "is implemented",
    "are implemented",
    "ensures",
    "guarantees",
    "proven",
)

# Requirement words (matched at the start of a word) that imply server-side processing.
DEFAULT_BACKEND_KEYWORDS: tuple[str, ...] = ("search", "upload")

# A phrase is not a claim when one of these immediately precedes it ("is not yet proven").
_NEGATIONS = ("not", "never", "not yet", "no", "yet to be")


@dataclass(frozen=True)
class LintConfig:
    product_terms: tuple[ProductTerm, ...] = DEFAULT_PRODUCT_TERMS
    wording_phrases: tuple[str, ...] = DEFAULT_WORDING_PHRASES
    backend_keywords: tuple[str, ...] = DEFAULT_BACKEND_KEYWORDS
    # Component types that count as "the frontend only" for the backend-processing rule.
    frontend_types: frozenset[NodeType] = field(
        default_factory=lambda: frozenset({NodeType.CLIENT})
    )

    def with_extra_terms(self, names: Iterable[str]) -> "LintConfig":
        known = {t.name.lower() for t in self.product_terms}
        extra = tuple(
            ProductTerm(n.strip()) for n in names if n.strip() and n.strip().lower() not in known
        )
        return LintConfig(
            self.product_terms + extra,
            self.wording_phrases,
            self.backend_keywords,
            self.frontend_types,
        )


class LintFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: LintRule
    severity: Literal["warning", "info"]
    message: str
    # Schema path of the item concerned, e.g. "recommended_stack[2]"; None for whole-proposal.
    path: str | None = None
    terms: list[str] = []


class LintReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = LINT_VERSION
    findings: list[LintFinding]


@dataclass(frozen=True)
class RequirementText:
    title: str
    description: str
    source_quote: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.description}\n{self.source_quote}"


def _term_patterns(config: LintConfig) -> list[tuple[ProductTerm, re.Pattern[str]]]:
    patterns = []
    for term in config.product_terms:
        spellings = sorted({term.name, *term.aliases}, key=len, reverse=True)
        body = "|".join(re.escape(s) for s in spellings)
        flags = 0 if term.case_sensitive else re.IGNORECASE
        patterns.append((term, re.compile(rf"(?<![\w.]){body}(?![\w])", flags)))
    return patterns


def _is_name_use(text: str, match: re.Match[str]) -> bool:
    """An ambiguous word that opens a sentence ("Express the data ...") is ordinary English
    unless the whole line is a short label such as a technology field."""
    start = match.start()
    line_start = text.rfind("\n", 0, start) + 1
    before = text[line_start:start].rstrip()
    if before and before[-1] not in ".!?:":
        return True
    line_end = text.find("\n", start)
    line = text[line_start : line_end if line_end >= 0 else len(text)]
    return len(line.split()) <= 3


def find_terms(text: str, config: LintConfig) -> set[str]:
    found = set()
    for term, pattern in _term_patterns(config):
        for match in pattern.finditer(text):
            if not term.case_sensitive or _is_name_use(text, match):
                found.add(term.name)
                break
    return found


def _walk_text(value: Any, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in ("source_passages", "retrieval"):
                continue
            yield from _walk_text(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_text(child, f"{path}[{index}]")


def _item_text(item: Any) -> str:
    parts = [item.name, item.decision, item.rationale]
    if isinstance(item, StackItem):
        parts += [item.technology, item.layer]
    return "\n".join(parts)


def _check_origin(
    content: ArchitectureContent,
    requirements: Mapping[uuid.UUID, RequirementText],
    config: LintConfig,
) -> list[LintFinding]:
    findings = []
    # A preference for a platform does not make its individual services client-selected.
    checked = {
        DecisionOrigin.REQUIRED_BY_CLIENT: (
            LintRule.ORIGIN_OVERCLAIM,
            "Labelled required by the client, but names a product or service "
            "that none of its cited confirmed requirements mention",
        ),
        DecisionOrigin.PREFERRED_BY_CLIENT: (
            LintRule.PREFERENCE_OVERCLAIM,
            "Labelled a client preference, but names a product or service "
            "that none of its cited confirmed requirements mention",
        ),
    }
    groups = (
        ("recommended_stack", content.recommended_stack),
        ("integrations", content.integrations),
        ("data_architecture", content.data_architecture),
        ("security_architecture", content.security_architecture),
        ("non_functional_design", content.non_functional_design),
    )
    for group, items in groups:
        for index, item in enumerate(items):
            if item.origin not in checked:
                continue
            rule, message = checked[item.origin]
            cited = [requirements[r] for r in item.supporting_requirement_ids if r in requirements]
            if not cited:
                continue  # nothing confirmed to compare with (stale citations show elsewhere)
            named = find_terms(_item_text(item), config)
            supported = find_terms("\n".join(r.text for r in cited), config)
            unsupported = sorted(named - supported)
            if unsupported:
                findings.append(
                    LintFinding(
                        rule=rule,
                        severity="warning",
                        message=message,
                        path=f"{group}[{index}]",
                        terms=unsupported,
                    )
                )
    return findings


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(rf"(?<![\w-])({words})(?![\w-])", re.IGNORECASE)


def _is_negated(text: str, start: int) -> bool:
    before = text[:start].rstrip().lower()
    return any(before.endswith(f" {n}") or before == n for n in _NEGATIONS)


def _check_wording(
    title: str, summary: str, content: ArchitectureContent, config: LintConfig
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    patterns = [(p, _phrase_pattern(p)) for p in config.wording_phrases if p.strip()]
    body = content.model_dump(mode="json", exclude={"title", "summary"})
    texts = [("title", title), ("summary", summary), *_walk_text(body, "")]
    for path, text in texts:
        hits = []
        for phrase, pattern in patterns:
            if any(not _is_negated(text, m.start()) for m in pattern.finditer(text)):
                hits.append(phrase)
        if hits:
            findings.append(
                LintFinding(
                    rule=LintRule.COMPLETED_IMPLEMENTATION_WORDING,
                    severity="warning",
                    message=(
                        "Reads as completed or guaranteed work; a proposal describes a design, "
                        "not a delivered system"
                    ),
                    path=path,
                    terms=sorted(hits),
                )
            )
    return findings


def _check_coverage(
    content: ArchitectureContent,
    requirements: Mapping[uuid.UUID, RequirementText],
    config: LintConfig,
) -> list[LintFinding]:
    matrix = content.coverage_matrix
    if not matrix:
        return []
    findings = []
    if all(e.status is CoverageStatus.ADDRESSED for e in matrix):
        findings.append(
            LintFinding(
                rule=LintRule.COVERAGE_ALL_ADDRESSED,
                severity="warning",
                message=(
                    f"All {len(matrix)} coverage entries are 'addressed' and none is partial "
                    "or unresolved; check that no gap was left out"
                ),
            )
        )
    kinds = {c.id: c.type for c in content.components}
    keywords = [
        (k.strip().lower(), re.compile(rf"\b{re.escape(k.strip())}", re.IGNORECASE))
        for k in config.backend_keywords
        if k.strip()
    ]
    for index, entry in enumerate(matrix):
        req = requirements.get(entry.requirement_id)
        if req is None or not entry.related_ids:
            continue
        needle = req.title + "\n" + req.description
        matched = sorted({word for word, pattern in keywords if pattern.search(needle)})
        if not matched:
            continue
        # Decisions and unknown ids are not frontend components, so they keep the rule quiet.
        if all(kinds.get(r) in config.frontend_types for r in entry.related_ids):
            findings.append(
                LintFinding(
                    rule=LintRule.FRONTEND_ONLY_BACKEND_REQUIREMENT,
                    severity="warning",
                    message=(
                        "A requirement that implies backend processing is mapped only to "
                        "frontend components"
                    ),
                    path=f"coverage_matrix[{index}]",
                    terms=matched,
                )
            )
    return findings


def _check_citations(content: ArchitectureContent) -> list[LintFinding]:
    info = content.retrieval
    if info is None or info.passages_provided == 0 or cited_passage_ids(content):
        return []
    return [
        LintFinding(
            rule=LintRule.PASSAGES_NOT_CITED,
            severity="info",
            message=(
                f"{info.passages_provided} document passage(s) were supplied but none is cited"
            ),
        )
    ]


def lint_proposal(
    title: str,
    summary: str,
    content: ArchitectureContent,
    confirmed_requirements: Mapping[uuid.UUID, RequirementText],
    config: LintConfig | None = None,
) -> LintReport:
    """Pure function of its arguments; the inputs are read, never modified."""
    config = config or LintConfig()
    findings = [
        *_check_origin(content, confirmed_requirements, config),
        *_check_wording(title, summary, content, config),
        *_check_coverage(content, confirmed_requirements, config),
        *_check_citations(content),
    ]
    return LintReport(findings=findings)
