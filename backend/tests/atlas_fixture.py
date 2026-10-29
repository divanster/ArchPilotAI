"""An Atlas-shaped architecture: diagram ids (n-*) differ from component ids (c-*)."""

import copy
import uuid
from typing import Any

from app.domain.architecture import ArchitectureContent, build_alias_map
from tests.test_architecture_api import _swap, valid_payload

REQ_IDS = [str(uuid.UUID(int=n)) for n in (101, 102)]
CONFIRMED = [uuid.UUID(i) for i in REQ_IDS]

COMPONENTS = ["Web UI", "API", "Database", "Identity provider", "Worker", "Queue", "Search"]
# (source, target) by diagram node id; "n-entra" is named "Microsoft Entra ID" and has no component.
EDGES = [
    ("n-web", "n-api"),
    ("n-api", "n-db"),
    ("n-api", "n-entra"),
    ("n-api", "n-queue"),
    ("n-worker", "n-queue"),
]
NODES = {
    "n-web": "Web UI",
    "n-api": "API",
    "n-db": "Database",
    "n-entra": "Microsoft Entra ID",
    "n-worker": "Worker",
    "n-queue": "Queue",
}


def atlas_payload(ids: list[str] | None = None) -> dict[str, Any]:
    p = copy.deepcopy(valid_payload(ids or REQ_IDS))
    template = p["components"][0]
    p["components"] = [
        {
            **template,
            "id": f"c-{n}",
            "name": name,
            "type": "database" if name == "Database" else "api",
        }
        for n, name in enumerate(COMPONENTS)
    ]
    p["diagram"] = {
        "nodes": [{"id": i, "name": name, "type": "api"} for i, name in NODES.items()],
        "edges": [{"source": s, "target": t, "relationship": "uses"} for s, t in EDGES],
    }
    decision = copy.deepcopy(p["integrations"][0])
    p["integrations"] = [{**decision, "id": f"d-int-{n}", "name": f"Int {n}"} for n in range(5)]
    p["coverage_matrix"] = [{**entry, "related_ids": ["c-1"]} for entry in p["coverage_matrix"]]
    return p


def atlas_content() -> ArchitectureContent:
    payload = atlas_payload()
    payload.pop("title", None)
    payload.pop("summary", None)
    return ArchitectureContent.model_validate(payload)


def atlas_alias_payload(ids: list[str]) -> dict[str, Any]:
    """The Atlas payload as a model answering the prompt returns it (requirement aliases)."""
    table = {str(rid): alias for alias, rid in build_alias_map(uuid.UUID(i) for i in ids).items()}
    result: dict[str, Any] = _swap(atlas_payload(ids), table)
    return result
