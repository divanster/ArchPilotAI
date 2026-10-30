"""A synthetic Atlas-shaped project in an isolated SQLite file.

The schema is created with `Base.metadata.create_all` (no migrations) and every row is written
through the regular services, so the agent sees what it would see after a real review flow:
confirmed requirements, an approved Atlas-shaped architecture and its graph, an approved plan
and one accepted baseline patch in the workspace. Seeding always uses the fake provider.
"""

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, update
from sqlalchemy.orm import Session

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import Settings
from app.domain.architecture import ProposalStatus, build_alias_map
from app.domain.patch import GeneratedFile, GeneratedPatch, PatchLimits
from app.llm.architecture_prompt import build_prompt, fake_architecture_answer
from app.llm.fake_provider import FakeLLMProvider
from app.models.base import Base
from app.models.document import Document
from app.models.plan import PlanTask
from app.models.project import Project
from app.models.requirement import ExtractionRun, Requirement
from app.repositories.architecture import ArchitectureRepository
from app.repositories.chunk import ChunkRepository
from app.repositories.decision import DecisionRepository
from app.repositories.document import DocumentRepository
from app.repositories.graph import GraphRepository
from app.repositories.patch import PatchRepository
from app.repositories.plan import PlanRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture import ArchitectureService, ProposalChange
from app.services.architecture_generation import ArchitectureGenerationService
from app.services.decision import DecisionService
from app.services.graph import GraphService
from app.services.patch import PatchService
from app.services.plan import PlanService

SEED_PROVIDER = "eval-seed"

# (type, title, source line). The code prefix of each line is what coverage reports cite.
REQUIREMENT_LINES: list[tuple[str, str, str]] = [
    ("business", "Faster order handling", "BR-001: Atlas must cut order handling time for sales."),
    ("functional", "Corporate sign-in", "FR-001: Users must sign in with Microsoft Entra ID."),
    ("functional", "Catalogue search", "FR-002: Users must search the catalogue by category."),
    ("functional", "Queued confirmations", "FR-003: Order confirmations must go via a queue."),
    ("non_functional", "Search latency", "NFR-001: Search must answer within two seconds."),
    ("constraint", "EU data residency", "CON-001: Customer data must be stored in the EU."),
]
DOCUMENT_TEXT = "Atlas order platform requirements.\n" + "".join(
    f"{line}\n" for _, _, line in REQUIREMENT_LINES
)

COMPONENTS = ["Web UI", "API", "Database", "Identity provider", "Worker", "Queue", "Search"]
# Diagram ids (n-*) differ from component ids (c-*); "n-entra" has no component.
NODES = {
    "n-web": "Web UI",
    "n-api": "API",
    "n-db": "Database",
    "n-entra": "Microsoft Entra ID",
    "n-worker": "Worker",
    "n-queue": "Queue",
}
EDGES = [
    ("n-web", "n-api"),
    ("n-api", "n-db"),
    ("n-api", "n-entra"),
    ("n-api", "n-queue"),
    ("n-worker", "n-queue"),
]

# Accepted before any evaluated run, so removals from accepted files can be measured.
BASELINE_FILES: dict[str, str] = {
    "src/atlas/shared.py": (
        '"""Shared helpers accepted before the evaluated runs."""\n\n\n'
        "class OrderStatus:\n"
        '    PENDING = "pending"\n'
        '    CONFIRMED = "confirmed"\n\n\n'
        "def order_reference(number: int) -> str:\n"
        '    return f"ATL-{number:06d}"\n\n\n'
        "def is_eu_region(region: str) -> bool:\n"
        '    return region.lower().startswith("eu")\n'
    ),
    "config/atlas.json": json.dumps(
        {"region": "westeurope", "search": {"timeout_seconds": 2}, "queue": "orders"},
        indent=2,
    )
    + "\n",
}


@dataclass(frozen=True)
class SeededProject:
    project_id: uuid.UUID
    plan_id: uuid.UUID
    task_keys: list[str]
    baseline: dict[str, str]


def make_engine(db_path: Path, settings: Settings) -> Engine:
    """A SQLite file for the evaluation. Refuses the configured development database."""
    target = db_path.resolve()
    if target == Path(settings.sqlite_path).resolve():
        raise ValueError("The evaluation database must not be the development database")
    target.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{target.as_posix()}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _swap(node: Any, table: dict[str, str]) -> Any:
    if isinstance(node, str):
        return table.get(node, node)
    if isinstance(node, list):
        return [_swap(n, table) for n in node]
    if isinstance(node, dict):
        return {k: _swap(v, table) for k, v in node.items()}
    return node


def atlas_answer(requirements: list[tuple[uuid.UUID, str]]) -> str:
    """The Atlas-shaped architecture reply, citing requirement aliases as a model would."""
    prompt = build_prompt(
        {"confirmed_requirements": [{"id": str(rid), "type": t} for rid, t in requirements]}, "n"
    )
    payload: dict[str, Any] = json.loads(fake_architecture_answer(prompt))
    template = payload["components"][0]
    payload["components"] = [
        {
            **template,
            "id": f"c-{n}",
            "name": name,
            "type": "database" if name == "Database" else "api",
        }
        for n, name in enumerate(COMPONENTS)
    ]
    payload["diagram"] = {
        "nodes": [{"id": i, "name": name, "type": "api"} for i, name in NODES.items()],
        "edges": [{"source": s, "target": t, "relationship": "uses"} for s, t in EDGES],
    }
    decision = copy.deepcopy(payload["integrations"][0])
    payload["integrations"] = [
        {**decision, "id": f"d-int-{n}", "name": f"Int {n}"} for n in range(5)
    ]
    payload["coverage_matrix"] = [
        {**entry, "related_ids": ["c-1"]} for entry in payload["coverage_matrix"]
    ]
    aliases = build_alias_map(rid for rid, _ in requirements)
    return json.dumps(_swap(payload, {str(rid): alias for alias, rid in aliases.items()}))


def _requirements(session: Session, project_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    data = DOCUMENT_TEXT.encode("utf-8")
    document = Document(
        id=uuid.uuid4(),
        project_id=project_id,
        original_filename="atlas-requirements.txt",
        media_type="text/plain",
        size_bytes=len(data),
        content_sha256=hashlib.sha256(data).hexdigest(),
        extracted_text=DOCUMENT_TEXT,
    )
    session.add(document)
    session.flush()
    extraction = ExtractionRun(
        id=uuid.uuid4(),
        project_id=project_id,
        document_id=document.id,
        provider=SEED_PROVIDER,
        model=SEED_PROVIDER,
        prompt_version=SEED_PROVIDER,
        status="succeeded",
    )
    session.add(extraction)
    session.flush()
    created: list[tuple[uuid.UUID, str]] = []
    for kind, title, line in REQUIREMENT_LINES:
        start = DOCUMENT_TEXT.index(line)
        requirement = Requirement(
            id=uuid.uuid4(),
            project_id=project_id,
            document_id=document.id,
            extraction_run_id=extraction.id,
            type=kind,
            title=title,
            description=line.split(": ", 1)[1],
            source_quote=line,
            source_start=start,
            source_end=start + len(line),
            status="confirmed",
        )
        session.add(requirement)
        created.append((requirement.id, kind))
    session.commit()
    return created


def patch_service(session: Session, settings: Settings) -> PatchService:
    """For review operations only: it never generates."""
    return PatchService(
        FakeLLMProvider(model=SEED_PROVIDER),
        ProjectRepository(session),
        PlanRepository(session),
        ArchitectureRepository(session),
        RequirementRepository(session),
        PatchRepository(session),
        limits=PatchLimits(
            settings.patch_max_files, settings.patch_max_file_bytes, settings.patch_max_patch_bytes
        ),
        max_output_tokens=settings.patch_max_output_tokens,
        max_context_chars=settings.patch_max_context_chars,
        decisions=DecisionService(
            DecisionRepository(session), ProjectRepository(session), ArchitectureRepository(session)
        ),
    )


def seed_project(session: Session, settings: Settings, name: str = "Atlas (eval)") -> SeededProject:
    project = Project(id=uuid.uuid4(), name=name, description="Synthetic evaluation project")
    session.add(project)
    session.commit()
    pid = project.id
    requirements = _requirements(session, pid)

    projects = ProjectRepository(session)
    architecture = ArchitectureRepository(session)
    reqs = RequirementRepository(session)
    generator = ArchitectureGenerationService(
        FakeLLMProvider(model=SEED_PROVIDER, architecture_text=atlas_answer(requirements)),
        projects,
        DocumentRepository(session),
        reqs,
        architecture,
        max_requirements=settings.architecture_max_requirements,
        max_context_chars=settings.architecture_max_context_chars,
        max_unconfirmed_chars=settings.architecture_max_unconfirmed_chars,
        max_output_tokens=settings.architecture_max_output_tokens,
        chunks=ChunkRepository(session),
    )
    proposal = generator.generate(pid)
    graph = GraphService(
        GraphRepository(session),
        architecture,
        projects,
        reqs,
        hub_threshold=settings.graph_hub_threshold,
    )
    ArchitectureService(architecture, projects, reqs, on_approved=graph.build).update(
        pid, proposal.id, ProposalChange(status=ProposalStatus.APPROVED)
    )

    plans = PlanService(
        PlanRepository(session), architecture, GraphRepository(session), projects, reqs
    )
    plan = plans.build(pid).plan
    # Gates that need a person (technology confirmation, blocking questions) are cleared so the
    # evaluation measures the agent, not the review workflow.
    session.execute(
        update(PlanTask).where(PlanTask.plan_id == plan.id).values(flags=[], blocking_questions=[])
    )
    session.commit()
    detail = plans.decide(pid, plan.id, "approved")
    tasks = sorted(detail.tasks, key=lambda t: t.order_index)

    patches = patch_service(session, settings)
    baseline = patches.propose(
        pid,
        detail.plan,
        tasks[0],
        GeneratedPatch(
            files=[
                GeneratedFile(path=path, change="create", content=content)
                for path, content in BASELINE_FILES.items()
            ]
        ),
        provider=SEED_PROVIDER,
        model=SEED_PROVIDER,
        prompt_version=SEED_PROVIDER,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        warnings=[],
        acknowledged=True,
    )
    patches.accept(pid, baseline.patch.id, "eval-harness", "Synthetic accepted baseline")
    return SeededProject(pid, plan.id, [t.key for t in tasks], dict(BASELINE_FILES))
